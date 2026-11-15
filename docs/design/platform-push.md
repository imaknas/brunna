# Platform push: a sandboxed project's agent says "push", the platform pushes

Status: design, 11-15; r1 the same day after a blind review, r2 after a second (below; where they and the first
version disagree, the text now says r1 and r2). The core is built (11-16, "As built" under r2); the grant and the
onboarding questions are built separately. It builds on `brunna push` (`bin/git_remote.py`; `git-broker.md`, "Owner-run push,
built first (11-15)") and takes over the push half of the git broker's slice d (section 7). Open questions for the
owner at the end.

**The owner's principle (11-15):** no person, neither the owner nor anyone else who runs brunna, should have to run
`git push`. A project's agent says "push" and the platform does it. People appear only when a rule can't be met.
**And (11-14):** a user never has to remember a special way to use their agent. Using their own agent tool as usual
should give them all of brunna. brunna is open source, so everything here has to work for other users and other forges
too.

## r1 (11-15), after a blind review

The review found 3 blocking points, 11 should-fix and 8 nits. All are folded into the sections below:
- **B1, scans that a merge or a NUL byte hid.** `git log -p` shows no diff for a merge commit by default, and a NUL in
  a file's first 8 KB turns its text into "Binary files differ". The scans now read every new **blob** (`rev-list
  --objects` → `cat-file --batch`), each as text, and every new commit's message and identities on their own (check 7).
- **B2, content that steered the scanner.** gitleaks skips a line carrying `gitleaks:allow` and reads
  `.gitleaksignore` from its cwd, and with gitleaks installed the built-in filename rules didn't run.
  - The marker is now neutralised in the input.
  - The built-in set always runs.
  - gitleaks runs in a fresh platform folder, with an empty ignore file and no baseline.
  - Refusals name the file and the commit, and never point at a way to suppress a finding (check 7, "The reply").
- **B3, `others` doesn't bound publication.** On a public remote every pushed branch is public, and `gh-pages`, a
  hosting "production branch" or preview deploys publish or build any branch, some with secrets. What `others` bounds
  is the default branch's CI and deploys. Rewritten in sections 2, 5 and 8 and open question 2. The non-interactive
  default for a public remote is now `none` plus a one-time `brunna push setup` to-do.
- **S1:** the owner form runs checks 5–9 too and shows each result as a flag. Only an interactive "y" overrides a
  refusal; `--yes` never does (section 3, "The forms").
- **S2:** a crashed worker. Each dispatch tick starts a worker for any queued request whose lock is free. A request
  past its deadline is `failed: worker lost`. Taking in an id already queued does nothing ("Who runs it").
- **S3:** the declared-secret values are read only from relative paths, without following links, from the project's
  own folders. Every other path is skipped without a word to the project (check 7).
- **S4:** **sessions only** is a rule again, as r3 decided. Requests go into a folder that only the project's session
  sandboxes can write, never its services, jobs or platform-settings runs ("Who may request"). Sessions that letters
  drive are a residual (section 8). `git-broker.md`'s appended section is corrected to match.
- **S5:** every to-do, rule and doc names the owner form as `brunna push --owner <project> <branch>`.
- **S6:** the client waits 90 s by default, under the agents' command timeouts.
- **S7:**
  - `connect.py` gains `--sandbox` and `--push`;
  - `connect_project` passes `--push none`;
  - the default follows check 8's anonymous probe (unknown counts as public), not `--public`;
  - the lease-runner upgrade path defaults to `none` plus a setup to-do.
- **S8:** raw git, ssh and credential-helper output never reaches the project. The project gets a classified reason,
  and the raw text goes to the owner's log and to-do.
- **S9:**
  - the bundle is capped near `max_total_mb`;
  - the checks run in a staging repo, and the owned repo fetches only after they pass;
  - size (check 9) runs before the scans;
  - the scan has a timeout;
  - concurrency is shared fairly per project.
- **S10:** the worker takes a blocking lock, with a bound.
- **S11:** email privacy (GH007) is per pushing user, and a deploy key has none. So for a public remote the default
  email list is the noreply address only.
- **Nits:**
  - an empty `emails` list raises an owner to-do pointing at `brunna push setup`;
  - the anonymous probe sets `GIT_CONFIG_NOSYSTEM=1` and `-c credential.helper=`;
  - Git LFS is detected and refused;
  - check 8 also refuses the owner's home path and user name on a public remote;
  - the locked-screen measurement is cited from `platform-credentials.md`, "The problem";
  - the grant lives in its own folder, outside `credentials.py`'s globs;
  - "daily limit reached" is `fix: wait`;
  - the "branch not allowed" to-do closes when an owner push lands that sha;
  - gitleaks without `stdin` gets a doctor row and `fix: owner`;
  - `merge-branch` treats "one in flight" as no failure;
  - a forge or organisation without deploy keys gets no platform push for that repo, never a fallback.

## r2 (11-15), after the second blind review

A second blind review of r1 found one blocking point and thirteen should-fix; all are built into the core (11-16):
- **K1, the request folder's own entry.** A session's allowWrite covers its folder as a subpath, so the session can
  swap the folder itself for a link (to `~/.ssh`, say), and take-in, as the owner, would read, delete and name what is
  there. Now:
  - the platform makes `<push_dir>` (0700) and each sandboxed project's folder (`push_queue.ensure_folder`) before it
    renders any session settings (render-project-settings, and every take-in);
  - every read, removal and write names the base `<push_dir>` and the relative `<project>/<name>` (nofollow), so a link
    at `<project>` is refused by O_NOFOLLOW;
  - take-in reads only `<id>.request.json` and never touches anything else there;
  - a folder that is missing, a link or not a folder is made again, and that is logged.
- **S-a, what the folder separates.** It separates session processes from services and jobs, not a session's intent
  from code the project wrote: without the git fence, a job can plant `.git` hooks, an fsmonitor or a Makefile that run
  `brunna push` inside a session. Sections 3 ("Who may request") and 8 say so.
- **S-b, objects read raw.** Checks 7 and 8 read every new commit object whole, every new tree's entry names and every
  new blob. A new commit with a header other than tree, parent, author, committer and a UTF-8 encoding is refused
  (gpgsig, mergetag, another encoding, anything unknown). Check 8 covers every new tree's paths, not `plan()`'s diff.
- **S-c:** the filename rules (and gitleaks' path context) run over every (path, blob) pair of the new trees, so a
  renamed old blob is caught.
- **S-d:** a blob is also scanned as UTF-16 (a byte order mark or alternating NULs) and with its NULs stripped.
- **S-e, declared values can't be turned off.** `secrets:` is the project's, so the check matches every value the
  platform has ever seen declared: salted hashes of values of 12 characters or more, kept in the platform's folder and
  never dropped when a declaration goes. A file with another hard link isn't read.
- **S-f:** take-in writes a fresh platform-made `requests/<id>.json` from the parsed fields, removes the original,
  refuses a file with another hard link, and the worker reads its copy with a cap.
- **S-g:** the worker pushes the requested sha, checked to be reachable from the branch, not the branch's tip. A queued
  request not yet started is replaced by a newer one for the same branch, which the worker checks descends from it, so
  `merge-branch`'s requests aren't lost.
- **S-h, S-i:** `session_write` is added in one place, the session rendering (`runtime_claude.session_settings`, called
  only by render-project-settings), as an allowWrite and as a root of `_protect_rules`. Never in `settings()` or
  `pol["write"]`, so not in `platform_settings`, leases, Seatbelt or Landlock.
- **S-j:** Codex has no session rendering yet (`runtime_codex.unconfined`), so its sessions can't ask
  (`session_write` port absent, its reason in doctor) until runtime-codex Step 4.
- **S-k:** a worker is live while its recorded pid runs with its recorded start time. One that died is started again
  once; the second death ends the request as `worker lost`. A request's age never does (a sleeping machine loses
  nothing).
- **S-l:** visibility is probed live at each push. On a public remote only noreply addresses and the owner's
  `public_emails` pass. A visibility different from the one `setup` recorded (`git_push.visibility`) raises the setup
  to-do, and a public remote recorded as private is refused.
- **S-m:** check 9 sums every new object's size, and a commit object is capped at 1 MB.
- **Nits:**
  - the worker maps every exception to a classified reason (no private paths reach the project);
  - dispatch tracks workers by pid, not by probing the lock;
  - requests are taken oldest first by mtime, not by the id's date;
  - results older than 7 days are removed;
  - the daily probe and dispatch: see "As built" below.

### As built (11-16)

The core is in `bin/git_remote.py` (forms, pipeline, worker, doctor rows), `bin/push_checks.py`,
`bin/push_queue.py` and `take_push_requests` in `bin/hubbus`; the tests are `tests/test_platform_push.py`. With the
grant (`bin/git_push_grant.py`, below):
- **the credential step** overlays `git_push_grant.ssh_env(project, run_dir)` last on an environment built from
  scratch (the key's copy in the run's own private folder, removed with it), and pushes to
  `git_push_grant.push_url(project)`; no module, or `NotSetUp`, refuses with an owner to-do, never a fallback
  to the owner's credential. A local-path origin needs no key;
- **check 8's and S-l's visibility** is the grant's anonymous probe (`git_push_grant.visibility`), a local path
  private;
- **`brunna push setup`** is the grant's (key, policy, the repo's identity); `git_remote.py push setup` hands over to
  it. Setup doesn't record `git_push.visibility` yet, so S-l's mismatch to-do waits for that;
- doctor shows the grant's `status` and probes each project's credential as the worker would.

Not in it:
- **the status-page rows** (section 9, step 5): doctor has the rows;
- **the owner form without an emails list:** check 6 isn't a refusal there (the owner form worked before the field
  existed); the plan still flags emails the remote hasn't seen;
- **the session's side of "who may request"** is checked in the rendered settings, and through the real tool by the
  conformance check `session-write` (it needs the tool's login, so not in the test suite).

## 1. Two axes, and when to sandbox

A project has two settings, and neither depends on the other:
- **type**: `idea` or `project`. An idea sandbox is the assistant's exploration and has no remote. `upgrade-idea`
  makes it a project, and the nightly `brunna/idea-upgrades` job lists the candidates (`idea-sandboxes.md`, "Growing
  up (11-15)").
- **sandbox**: `enforce.sandbox`, an owner-only security field (`registry.PLATFORM_OWNED`). A sandboxed project's
  sessions, services and jobs write only its own folders, read no other project and no credentials, and reach only
  the hosts in its list.

**Unsandboxed projects don't change.** Their agents push with the owner's gh or ssh, as they do now. Everything below
is for sandboxed projects only.

**When to sandbox.** Sandbox a project when its agent:
- **would touch other people's data:** colleagues' records, other people's documents or profiles, recordings of
  others. A sandbox keeps a mistake, or an instruction planted in that data, inside the project;
- **runs or takes in untrusted code or content:** downloaded models, benchmark code from others, unattended
  processing of external papers, pages or mail. Content like that can carry instructions, and a sandboxed agent that
  follows them can't reach the owner's credentials, other projects or the bus.

**Maturity is no reason either way.** An idea isn't sandboxed because it is new. A project doesn't drop its sandbox
because it grew up. `upgrade-idea` keeps the sandbox, and turning it off stays the owner's own edit.

**Once the platform pushes, sandboxing costs nothing in pushing.** What a sandbox still costs (section 8):
- `gh`'s forge actions (pull requests, issues, releases);
- hosts outside the network list;
- programs that need a capability (`brunna cap-run`).

## 2. Onboarding asks

Every way a project comes in asks two questions, with the criterion from section 1 in the text:
1. **Sandbox it?** "Will its agent touch other people's data, or run or read untrusted code or content (downloaded
   models, others' benchmarks, unattended processing of outside content)?"
2. **If sandboxed, which branches may the platform push for it?**
   - `none`: you push yourself;
   - `others`: every branch but the default;
   - `all`: the default branch too.

   On a public remote the question also says (B3): "every branch you allow is published as soon as it is pushed, and
   a site or deploy that builds from branches (`gh-pages`, a hosting 'production branch', preview deploys) runs it.
   `others` keeps the default branch's CI and deploys yours, not the content private."

Answers go into the registry as owner-only fields (section 5):
- **`brunna connect`** (`bin/connect.py`) asks both questions per project, in its existing UI, before it calls
  `onboard-project.sh`. It gains `--sandbox` / `--no-sandbox` and `--push none|others|all` for runs without a person
  (S7).
- **`onboard-project.sh`** stays non-interactive and gains flags:
  - `--sandbox` / `--no-sandbox`;
  - `--push none|others|all`;
  - `--push-email <address>` (repeatable).

  It writes the answers through `ONBOARD_REGISTRY_EXTRA`, as `create-idea-sandbox.py` does.
- **The `new-or-onboard-project` skill** gains a "Decision 5: sandbox and push". It asks the owner in the
  conversation, with the criterion and B3's warning, and passes the flags. Like any onboarding the skill runs, the run
  is in the owner's own session, where the owner answers.
- **`create-idea-sandbox.py`:** no change. An idea is always sandboxed, and with no remote its push stays `none`.
- **`upgrade-idea.py`** gains `--push none|others|all`. It already creates the GitHub repo with the owner's `gh` and
  makes the first push, so it is where the push field is set. Its default is below. **The lease-runner endpoint**
  (`POST /v1/sandboxes/<name>/upgrade`), where the assistant asks, defaults to `none` and raises the setup to-do
  (S7).
- **The MCP server's `connect_project`**, which an agent calls, may set `sandbox: true`, since that only narrows. It
  always passes `--push none` (S7). The setup to-do then names the one command.

**Non-interactive defaults** (flags absent, or `--yes`):
- sandbox: **off**, as today. A script can't judge the criterion, and nothing changes for anyone who doesn't ask;
- push for a sandboxed project, by the remote's visibility as check 8's anonymous probe sees it (S7; never by
  `--public`, which says only how a repo was created):
  - private: **`all`**;
  - public, or can't tell: **`none`**, plus a one-time owner to-do, "decide what the platform may push for <project>:
    `brunna push setup <project>`" (B3; open question 2). Deciding once at onboarding isn't a push;
- no remote: `none` (the registry refuses anything else);
- emails: check 6.

## 3. The platform pushes on a session's request

### The command

In a sandboxed project's folder, a session runs:

    brunna push [<branch>] [--no-wait | --wait <seconds>]

**The request.** `brunna push` resolves the branch (the one checked out in the worktree it runs in, unless named) and
that branch's commit with git's read-only commands. It then writes one file into the project's **session request
folder**, `<push_dir>/<project>/` (S4: "Who may request" says why there). The file is written as a dot-name `.tmp`
first and then renamed, as `hub-run` does:

    <id>.request.json: {"v": 1, "id": "m<MMDDhhmm>-<12 hex>", "branch": "<name>", "sha": "<40 or 64 hex>", "session": "<id>"?}

- Only `branch` and `sha` are read as the request.
- The project is the folder's own: a name in the file is never read.
- The `session` is kept only when `hubbus.own_session` confirms it, and is used only for the log.
- Nothing in the file widens what the registry allows: no flag, no remote, no destination ref.

**The forms of `brunna push`,** decided in this order:
1. **`brunna push setup <project> …`:** the owner's setup (section 6). `setup` is a reserved word, never read as a
   project.
2. **`brunna push --owner <project> [<branch>] [--yes]`:** the owner form (today's command), from anywhere. **Every
   to-do, rule and doc names it this way (S5).** The plain form below is refused inside any registered project's
   folder, brunna's own checkout included.
3. **The cwd is inside a registered project's workspace or runtime folder** (found by folder, as `hub-run`'s
   `find_workspace` does):
   - sandboxed: **the request form**, and the one positional argument is a branch;
   - unsandboxed: "push with git as usual; for another project use `brunna push --owner`", exit 2.
4. **Anywhere else:** `brunna push <project> [<branch>] [--yes]`, the owner form as before.

**The owner form runs the checks too (S1).**
- It runs checks 5–9 and shows each result as a flag above the commits. `plan()` lists only 50 commits and 100 paths;
  the checks cover all of them.
- A failed check is a refusal that only an interactive "y" to "push anyway?" overrides. `--yes` never overrides one: it
  answers only the plain question for a push with every check passed.
- **It doesn't work from a sandboxed session,** though the permission rule lets `brunna push` through. The rule only
  skips the prompt; the command still runs inside the session's sandbox. The owner form has to make and write
  `<state>/git-broker/<project>/`, which no sandbox can write (`policy.git_broker_dir`). So it checks first that it can
  write there, and otherwise refuses before contacting anything: "run it in your own terminal, outside the project's
  sandbox".

**Permissions.**
- `push` joins `policy.SESSION_SUBCOMMANDS` and `agenthub`'s `SESSION_COMMANDS`, so the permission rules let `brunna
  push` and `agenthub push` through.
- With the session request folder (below), this changes every sandboxed project's rendered settings once. That is
  checked with `render-project-settings.py --report` before the merge, and nothing else in the renderings may change.

**Waiting (S6).**
- The client waits up to `--wait` for its result file, `<push_dir>/<project>/<id>.result.json`. The default is **90 s**,
  under the agents' own command timeouts (Claude Code's Bash defaults to 120 s). The platform writes the file with
  `nofollow.replace`, as it writes the mailbox view.
- It prints the result and exits:
  - 0: pushed, or nothing to push;
  - 2: refused;
  - 1: failed;
  - 75: no answer yet ("the push goes on; its outcome comes as a letter").
- `--no-wait` exits 75 at once.

### Who may request: the project's sessions (S4)

r3 question 2 decided "sessions only" (`git-broker.md`, r3's list and slice d), and it still holds. The first version
of this doc said no file channel could enforce it. One can:
- **The session request folder.** `<push_dir>/<project>/` sits under a new root, `push_dir` in `brunna.json` (default
  `~/.local/state/brunna-push/`, beside the bus's default folder). It is outside every workspace, the hub root and the
  instance directory, so no deny has to be narrowed.
  - A new policy key, `session_write`, carries `<push_dir>/<project>/`.
  - **Each runtime adapter renders it only into the project's session sandbox:** Claude Code's settings `allowWrite`,
    Codex's writable roots. A runtime that can't add a writable folder says "push requests: not supported for this
    runtime's sessions" (`runtime_ports.Unsupported`, doctor's feature table).
  - **Left out everywhere else:**
    - `sandboxed-run`'s profiles, so services, command jobs and cap-run (Seatbelt and Landlock both list only what
      they allow, so leaving it out is enough);
    - every `settings: platform` run: agent jobs, event agent jobs, confined runs, the mailbox's background branches
      and its headless agent;
    - `brunna agent` calls.
  - Other projects' folders are in every sandbox's `deny_read`, as their other folders are.
- **Why not inside the workspace** (for example `.git/brunna/push-requests/`): services and jobs write the workspace.
  Carving a folder out of it is easy in Seatbelt. Landlock can only allow, so it needs `policy.beside`, which lists
  only the entries that exist now. A service then couldn't create anything new in the workspace's top folder, in
  `.git/` or in `.git/brunna/`, and git in a service needs `.git/index.lock`. That cost is too high.
- **Unsandboxed projects** run with the owner's authority, as `bus-sender.md` keeps them. They could write any project's
  folder, as they could write the registry. Nothing here changes that.
- The client also refuses in a confined run (`names.env("CONFINED")`) and for an agent the platform sends letters for
  (`propose_apply.py`). This is guidance only; the folder is the boundary.
- **Residual:** a session that a letter woke, or that acts on a letter, is a session. Anything that can mail the
  project can ask it to push (section 8).
- **What the folder separates (r2 S-a):** session processes from services and jobs, not a session's intent from code
  the project wrote. Without the git fence (`enforce.git: platform`), a job can plant `.git` hooks, an fsmonitor or a
  Makefile that a session then runs, and that code runs `brunna push` inside the session. The checks, not the folder,
  are what bound such a push.

### Who runs it, and why there

**Taking the request in (S2, S9).** `hubbus dispatch` (service `local.brunna-mailbox-dispatch`, every minute, as the
owner) reads every outbox in `take_outboxes`. It gains `take_push_requests`, which reads each sandboxed project's
`<push_dir>/<project>/` the same way: `nofollow`, regular files only, at most 64 KiB, oldest name first.
1. It checks the shape: `branch` text, `sha` hex, the id in the outbox id form.
2. **An id already in `<state>/git-broker/<project>/requests/` or in `pushes.jsonl` does nothing.** The request file
   is removed, and the earlier result stands. `take_file` may run twice over one outbox file (`bin/hubbus`, near line
   3376), and so may this.
3. It applies the limits below. A refusal writes the result file at once.
4. It moves the request into `<state>/git-broker/<project>/requests/<id>.json` (`policy.git_broker_dir`), which no
   sandbox can write.
5. **On every tick:** for each project with a queued request whose lock is free, it starts a worker. So a worker that
   died is replaced on the next tick.
6. A queued request older than `git_remote.TIMEOUT_S` plus 15 minutes, with no worker holding its lock, is ended as
   `failed: worker lost` (`fix: wait`, ask again).

A malformed file is refused through a result file when its id can be read, and through an `info` letter like
`outbox_refused`'s when it can't. A push request is not a letter: it never enters the bus's message table, so no
mailbox handler, wake or round limit is involved.

**The worker:** `git_remote.py serve <project>`.
- It runs as the owner, outside every sandbox.
- **It takes the project's lock blocking (S10)**, bounded by `TIMEOUT_S`, unlike the owner form's `locked()`, which
  refuses at once. While the owner pushes the same project, the request waits. Past the bound it stays queued for the
  next tick.
- It serves its project's one queued request.
- It is started like the mailbox's background runs (`subprocess.Popen(..., start_new_session=True)`, `bin/hubbus` near
  line 2565), with the double fork and `setsid` that `run-agent-job.sh` uses. A plain child of the dispatch tick dies
  when launchd ends that tick's process group (`known-issues.md`, "when a launchd job ends").
- A push can take up to `git_remote.TIMEOUT_S` (30 min), so it can't run inside the tick, which has to stay quick for
  mail.

**Why not the alternatives:**
- **An event job:** the scheduler ticks every 15 minutes, which is too slow for an agent waiting on "push". It would
  also need a new bus event type for a request, plus cursor state for what is a one-shot command.
- **cap-runner:** it is in the optional `sandbox` module, so sandboxed projects on an install without it couldn't push.
  Its request folder, `.hub-run/`, is in the workspace, which services write, so it couldn't be sessions only. Its jobs
  are runs under the platform's Seatbelt profile or model calls, and a push is neither.
- **The dispatch tick itself:** see above. It must not block mail.

### The pipeline

The worker runs `brunna push --owner … --yes`'s steps, with the owner's look replaced by the checks in section 4 and
the objects checked before the owned repo takes them (S9). In order:
1. **The registry:** `isolation.sandboxed`, `check_origin(paths.origin)`, and `enforce.git_push` (section 5) allows this
   branch. `valid_branch` checks the name.
2. **The remote:** `remote_heads` and `fetch_remote` in the owned bare repo. A failure here says which: the default
   branch can't be told, the remote is empty, or the remote can't be reached.
3. **The bundle:** made as the project (`project_git.job` `bundle-branch`, sandboxed), capped at `max_total_mb` plus a
   quarter instead of `project_git.MAX_BUNDLE`. The bundle is compressed, so anything larger fails check 9 anyway.
4. **Staging, checks, then the owned repo (S9):**
   - The bundle goes into `in.git`.
   - `checked.git` fetches from `in.git` over `file://` with `transfer.fsckObjects`. Both repos borrow the owned
     repo's objects, as `bundle_in`'s staging repo does.
   - **The bundled tip must equal the request's `sha`.** If the branch moved after the request, it is refused ("ask
     again").
   - The fast-forward and every check in section 4 run in `checked.git`.
   - Only when all pass does the owned repo fetch from `checked.git`. A refused push leaves nothing in the owned repo,
     and both staging repos are removed.
5. **The push:** `--force-with-lease=refs/heads/<branch>:<old>`, the exact sha (`git_remote`'s step 6, unchanged).
6. **The record:** a line in `pushes.jsonl` with `requested_by` (the confirmed session) and `checks` (each check's
   result). Then the result file and the letter.

**The credential step** is one function, `push_credential(project) -> env`, and nothing else depends on which
credential it gives.
- Until the owner answers open question 1, it is what `brunna push` uses: the owner's git environment (`KEEP_ENV`) as
  the dispatch service has it. That is the same credential `mirror-backup.sh` fetches with every night and
  `upgrade-idea.py` pushes with.
- Under launchd or systemd that environment is not a terminal's:
  - a Keychain-stored helper (`osxkeychain`, or `gh`'s default storage) can't be read while the screen is locked
    (`platform-credentials.md`, "The problem", measured 11-14 at 02:30) or over SSH (`known-issues.md`, "claude under
    SSH is 'not logged in'");
  - a file-stored `gh` token (`gh auth setup-git`) or an ssh key works.
- **Doctor and a daily probe** (part of `brunna/secrets`) run `ls-remote` exactly as the worker would, from the service
  environment, for each project with `git_push` set.
- A failure is an owner to-do, one per episode (the `credentials.py` pattern: `owner_notify`, `raise_todo`). There is
  never a fallback to another credential.

### Limits

- **One in flight per project, and a queue of one.** A request while another is queued or running is refused: "one
  push at a time; <id> is pending" (`fix: wait`).
- **Per project per day:** `git_push.requests_per_day` in `brunna.json` (default 20), refusals included, counted in the
  platform's folder (`fix: wait`).
- **Across projects (S9):** `git_push.concurrency` workers at once (default 2), with at most one per project. When more
  wait, the project served least recently goes first.
- **Time:**
  - the scans: `git_push.scan_timeout_s` (default 300). Past it, the scan refuses as "the scanner failed";
  - the push itself: `TIMEOUT_S`.
- **A request the worker reaches late** (the machine slept) is still served. The sha pin keeps it exact.

### The reply

**Every outcome with a valid id writes the result file:** a refusal at take-in as much as a push. A waiting client never
sits out its whole wait for a refusal.

    {"v": 1, "id", "state": "pushed|nothing|refused|failed", "branch", "old", "new", "commits",
     "check": "<the check that refused, or null>", "reason": "<plain text>", "fix": "project|owner|wait", "todo": "<key>"?}

**The letter:** from brunna to the project, type **`info`**. The text is the outcome in one line, plus the reason and
what to do.
- It goes to the project, not a session: `hubbus` addresses a session only through a reply to a letter that session
  sent, and a request is no letter.
- The requesting session hears the outcome through its client. The letter is for everyone else, and for the requester
  once its wait has ended.
- It is a notice, never a `request`. A `request` would start mailbox handlers, wake sessions and count against the round
  limits, all for the project's own action. If nobody is open, the letter waits in the inbox like any other.

**The project gets a classified reason, never raw output (S8).** The output of git, ssh and the credential helper can
name the owner's helpers, key files, host, proxy or account. The project gets the check and a fixed reason ("the remote
refused the push: protected branch"). The raw text, through `visible()`, goes only to `pushes.jsonl` and to the owner's
to-do.

**Refusals the project can fix** say how (`fix: project`), and nothing more happens:
- not a fast-forward ("merge or rebase onto origin/<branch>");
- the branch moved since the request;
- an author or committer email not in the list (check 6: "re-author the unpushed commits");
- a finding of checks 7 or 8. The reply names the commit and the path (blob) or "message", plus the rule, never the
  value. It says the history has to change: rewrite the unpushed commits so the content never appears. It doesn't
  mention any way to mark a finding as allowed (B2). If the project thinks it is a false positive, it asks the owner,
  who can push with `brunna push --owner` after looking;
- size or Git LFS (checks 9 and 4);
- a CI path changed without `git_ci` (check 5);
- GitHub's secret push protection (GH013).

**Wait only** (`fix: wait`): one in flight, the daily limit, a lost worker.

**Failures only the owner can fix** become an owner to-do (`fix: owner`). The to-do is sent with `owner_notify`, one per
cause and project per episode, carries the raw output, and its key is in the result:
- **the field doesn't allow the branch** (`none`, or the default branch under `others`). The to-do names the range and
  `brunna push --owner <project> <branch>`. It closes when an owner push lands that sha (`pushes.jsonl`);
- **`git_push.emails` is empty or missing:** "list the emails <project> commits with: `brunna push setup <project>`";
- **the credential** can't reach the remote (auth, an unknown host key), or the forge has deploy keys off (open
  question 1);
- **the remote is empty** (its first push isn't this command's);
- **the remote's own rules** refused it: a protected branch, or GitHub's email privacy (GH007);
- **`paths.origin`** is missing or refused;
- **gitleaks is installed but can't do `stdin`** (older than 8.19): doctor shows the row too;
- **three failures in a row** the platform can't classify.

`merge-branch` (section 5) treats a refusal of "one in flight" as no failure: it says the merge is in and the push is
pending.

## 4. The checks that replace the owner's look

Each check fails closed: when it can't run, or can't tell, the push is refused and the reason names the check.
- **What they read:** `checked.git`, on the commits `<new> --not <remote tips>` and on the objects only they bring
  (`rev-list --objects <new> --not <tips>`). That is every new commit and blob, not the 50 commits `plan()` shows.
- **Run order:** 1–4, then 9, then 5–8. A large push is refused before anything scans it (S9).

1. **Origin:** `check_origin` as built (`paths.origin` only; no ext/fd, no local path a sandbox can write, no temp
   area).
2. **Ref shape:** one branch, no tags, no deletes, no refspec, no force (`branch_arg` as built). The branch is allowed by
   `enforce.git_push.branches`.
3. **Fast-forward and pin:** `old` is an ancestor of `new`. The lease pins `old`, and the bundled tip equals the
   request's sha.
4. **Objects:**
   - git's fsck in `checked.git` (`hasDotgit` an error), as built;
   - a new gitlink (mode 160000) is refused, since there are no submodules (r3 question 5);
   - **Git LFS is refused (nit):** a new blob that is an LFS pointer (`version https://git-lfs.github.com/spec/v1`), or a
     new `.gitattributes` blob with `filter=lfs`. LFS objects never travel in a bundle, so no `brunna push` could carry
     them.
5. **CI files:**
   - The pushed commit's `.github` entry must equal the remote default branch's (`github_entry`, as built), unless
     `enforce.git_ci: true`.
   - The other forges' CI entries at the root are compared the same way: `.gitlab-ci.yml`, `.circleci`, `.buildkite`,
     `.travis.yml`, `Jenkinsfile`, `azure-pipelines.yml`, `bitbucket-pipelines.yml`, `.gitea`, `.forgejo`,
     `.woodpecker`, `.woodpecker.yml`. Nobody reviews these, so the list errs wide.
6. **Identity:**
   - Every new commit's author email (`%ae`) and committer email (`%ce`) must be in `enforce.git_push.emails`.
   - An empty or missing list refuses, with the owner to-do above.
   - The default is proposed at onboarding and written into the field, never computed at push time:
     - **for a private remote:**
       - the owner's `git config --global user.email`, read by onboarding in the owner's own process;
       - on GitHub, when the owner's `gh api user` answers, also `<id>+<login>@users.noreply.github.com`;
       - on other forges, the global email only, and the owner adds that forge's private address if they use one;
     - **for a public remote, the noreply address only (S11):** GitHub's email privacy (GH007) is a setting of the
       pushing user, and a deploy key has no user, so nothing at GitHub would stop a real address being published. On a
       forge without a private address, the list stays empty until the owner names one in `brunna push setup`.
7. **Secrets in the pushed objects (B1, B2):**
   - **Input:**
     - every new blob, read with `cat-file --batch` from `checked.git`, decoded as UTF-8 with errors replaced, whatever
       its bytes (a NUL makes nothing "binary" here);
     - separately, every new commit's message, author and committer.

     A secret added in one commit and removed in the next is still a new blob. A merge commit's content is in its
     blobs. The blob's path is the one `rev-list --objects` gives, for the reply and the filename rules. The size is
     already bounded by check 9.
   - **The input can't steer the scan (B2):**
     - every `gitleaks:allow` in the input is rewritten (to `gitleaks-allow-neutralised`) before any scanner sees it;
     - gitleaks runs with its cwd in a fresh, empty platform folder, with `--gitleaks-ignore-path` naming an empty
       platform file, no baseline, its default rules from a platform-owned `--config` path, and an environment built
       from scratch (no `GITLEAKS_CONFIG`);
     - no scanner reads the project's folder, so its `.gitleaks.toml`, `.gitleaksignore` and `.gitattributes` are never
       read.
   - **gitleaks, when installed:** `gitleaks stdin --redact --no-banner` on each blob, or on blobs joined with a header
     the platform maps findings back through. It needs version 8.19 or newer for `stdin` (unmeasured; an older one is
     `fix: owner`, above). A finding refuses. Any other exit, or the scan timeout, refuses as "the scanner failed",
     never as a pass.
   - **The built-in set, always**, gitleaks or not (`bin/push_checks.py`, high-confidence only):
     - private key blocks (`-----BEGIN … PRIVATE KEY-----`);
     - AWS key ids (`AKIA`/`ASIA` + 16);
     - GitHub tokens (`gh[pousr]_…`, `github_pat_…`);
     - Slack (`xox[abprs]-…`), Google API keys (`AIza…`), Stripe live keys (`sk_live_`, `rk_live_`), Anthropic
       (`sk-ant-`), OpenAI (`sk-proj-`);
     - new blobs at paths named `.env` or `.env.*` (not `*.example`, `*.sample` or `*.template`), `*.pem`, `*.p12`,
       `*.pfx`, `*.key`, `id_rsa*`, `id_ed25519*`.
   - **The project's declared secret values, always (S3):**
     - only `secrets:` entries without `kind` whose `path` is relative;
     - each read with `nofollow.read` from the project's own workspace or runtime folder (the base `registry` gives
       that entry): no link followed, regular files only, at most 64 KiB;
     - the whole file, or an `env_key`'s value; only values of 12 characters or more count, and matches are reported by
       name, never by value.

     **Skipped without a word to the project:** an absolute or `~/` path, a link, anything not a regular file. Such
     entries are project-settable, and reading them as the owner (`~/.config/gh/hosts.yml`, say) would turn a refusal
     into an oracle on files the sandbox can't read. A skip is logged for the owner only.
   - **What it can't catch:**
     - secrets that are encoded, split, encrypted or compressed;
     - formats outside the rules;
     - a secret the project made up that isn't declared;
     - personal data, which has no pattern (names, addresses, a colleague's document): see section 8.
8. **Private names and the owner's paths (public remotes):**
   - **When the remote is public,** neither the new blobs nor the new commits' messages, author or committer names, nor
     the changed paths may contain:
     - a project from the instance's `private-names.txt` (`private_names.found`), other than this project's own name;
     - **(nit)** the owner's home path (`$HOME`, and `/Users/<user>` or `/home/<user>`) or the owner's user name as a
       path segment.
   - **Public** means an anonymous `ls-remote` of the origin's https form works. It runs with `GIT_CONFIG_NOSYSTEM=1`,
     `GIT_CONFIG_GLOBAL=/dev/null`, `-c credential.helper=`, `GIT_TERMINAL_PROMPT=0` and a throwaway HOME (nit). The
     https form is the URL itself, or the mechanical one for `host:path` and `ssh://`. A local path counts as private.
   - **Can't tell** (no https form, or the network failed): treated as public. The cost is a false refusal, never a
     leak.
   - The registry records no visibility (`onboard-project.sh` step 11 writes none). The live probe stays right when the
     owner flips a repo's visibility.
   - A list that is named but unreadable (`private_names.ListError`) refuses. A deployment with no list checks only the
     owner's paths.
9. **Size:**
   - at most `git_push.max_commits` new commits (`brunna.json`, default 500);
   - each new blob at most `enforce.git_push.max_file_mb` (default 10, at most 100, under GitHub's 100 MB refusal);
   - all new blobs together at most `git_push.max_total_mb` (default 200);
   - the bundle capped near that (pipeline step 3).

   Measured with `rev-list --objects` and `cat-file --batch-check`. Binary or not doesn't matter: size does.

**Not a check, on purpose: the project's tests.** `brunna test`'s recorded passes live in the project's git dir, which
the project can write, and its tests are its own code. They show quality, not safety, and `merge-branch` already runs
them before anything lands on main.

## 5. Registry fields

Owner-only, under `enforce` (already in `PLATFORM_OWNED`, so a `manifest.update` that touches them is refused whole):

    enforce:
      sandbox: true
      git_ci: false                      # as built
      git_push:
        branches: all                    # none | others | all
        emails: [1234+me@users.noreply.github.com]
        max_file_mb: 10                  # optional, 1–100

- **`branches`:**
  - `none`: the platform pushes nothing. This is today's behaviour, and an absent `git_push` means the same;
  - `others`: any branch on `paths.origin` but the default (the remote's `HEAD`, and the registry's branch when it
    names one);
  - `all`: the default branch too.
- **What `others` bounds, and what it doesn't (B3).** It keeps the default branch's CI runs and deploys, and whatever
  else the forge treats the default branch as (the repo's front page, releases), for the owner. It does not keep
  content private:
  - on a public remote every pushed branch is public the moment it lands;
  - a forge or host can be set to build or publish other branches: `gh-pages`, a hosting service's "production
    branch" set to a non-default branch, preview deploys of every branch, which may run with the deploy's secrets.

  Whether a project's content may be published is decided by the remote's visibility and by `none` (open question 2),
  not by `others`.
- **Validation** (`registry.validate_enforce`), for anything but `none`:
  - needs `enforce.sandbox: true`;
  - needs a `paths.origin` that `check_origin`'s syntax accepts;
  - the project may not be in `service_sandbox_exempt` (its jobs, the bundler's included, run unsandboxed: the owner
    form refuses those too);
  - `emails` must be a list of `local@domain`, one line each, printable (empty is allowed by validation and refused at
    push time with the to-do);
  - `max_file_mb` must be an integer from 1 to 100;
  - unknown keys are refused.
- **The 11-15 decision** (r3 question 2, slice d) was "sessions only; the project's own non-default branches; no tags;
  `.github/**` only by the registry; `--force-with-lease` there":
  - **"sessions only" stands** (S4), enforced by the session request folder;
  - **"non-default branches only"** becomes the owner's per-project `branches`, and `others` is the old rule;
  - no tags, `.github/**` only with `git_ci`, and no signing stand;
  - `--force-with-lease` on own branches does not: this push is fast-forward only (section 8).
- **`push_on_merge`** stays the project's own field. In a sandboxed project, `merge-branch`'s step 5 (it runs in the
  session) writes a push request for the default branch instead of running `git push`, which can't reach a credential
  there anyway. Whether that request lands is the owner's `git_push.branches`. "One in flight" isn't a failure of the
  merge.
  - Slice d's rule that `validate_enforce` refuses `push_on_merge` with `enforce.git: platform` is no longer needed. It
    existed because the broker's own push would have obeyed `push_on_merge`.
- **Machine-wide settings** go in `brunna.json` and `contracts/hub.v1.schema.json`, as `git_broker.*` do: `push_dir`, and
  `git_push: {requests_per_day, concurrency, max_commits, max_total_mb, scan_timeout_s, wait_s}`.

## 6. Existing projects

- **Nothing changes until the owner sets the field.** An existing sandboxed project has no `git_push`, so it behaves as
  `none`:
  - a request from it is refused with the owner to-do that names `brunna push --owner <project> <branch>`;
  - the owner form works as today, plus the checks' flags (S1).
- **`brunna push setup <project> [--branches none|others|all] [--email <address>…]`** is owner-run, in a terminal or
  the brunna session:
  1. it runs check 8's anonymous probe and says whether the remote is public, with B3's warning when it is;
  2. it proposes the emails (check 6's defaults, plus the remote default branch's recent author emails), shown for the
     owner to pick;
  3. it runs the probe of section 3 (and, with the grant, makes it), so a credential that can't reach the remote shows
     up now and not at the first request;
  4. it writes the field with `commit-hub-files.sh`, as every script-written registry change, and closes a pending
     setup to-do.
- **The owner's decision for this deployment (11-15):**
  - All six existing sandboxed projects keep the sandbox. Each meets section 1's criterion: other people's data, or
    downloaded or external content run or processed unattended. One is kept as the sandbox's test bed.
  - The five with a remote get `branches: all` once this lands.
  - The sixth is still an idea with no remote and has nothing to push (`none`; validation refuses more).
  - None of the five has a CI workflow today (no `.github/workflows`), so `git_ci` stays unset.
  - Their names are in the instance's registry only, never here.
- **The rules text** (`templates/agent-instructions.md`): the line "In a sandboxed project pushing is {{owner}}'s
  alone: … ask {{owner}} to run `brunna push <project> <branch>`" becomes:

  > In a sandboxed project, push with `brunna push [<branch>]` from this session, never `git push`: the platform checks
  > and pushes it, and tells you why when it doesn't. If it says only {{owner}} can do it, {{owner}} has a to-do
  > already.

  - `sync-local-setup` renders it at each project's next session.
  - `templates/rules/idea-sandbox.md` keeps "no remote, no push" for ideas.
- **`docs/commands.md`** ("Pushing a sandboxed project") changes with the build: `brunna push --owner …` for the owner,
  `brunna push` for sessions, the checks' flags, and `--yes` overriding nothing (S1, S5).

## 7. Relation to the git broker

**What this supersedes:**
- **Slice d's session push mechanism:**
  - the session socket's `push`;
  - "the project's own non-default branches" as a fixed rule;
  - `--force-with-lease` there;
  - slice d's step 4 for pushes (P1 updates the remote-tracking ref). The project's `origin/<branch>` still catches up
    at its own next fetch.

  The request comes through the session request folder, the policy is the owner's per-project field, and the mechanics
  are `git_remote.py`'s, already built and tested. "Sessions only" is kept, by the folder (S4).
- **`idea-sandboxes.md`'s "Pushing from an upgraded sandboxed project … waits on the git broker"** is answered here.
  "Only main goes to the public remote" becomes `branches`, B3's default and check 8.

**What the broker still gives:**
- **The fence.** A sandboxed project's processes can write its `.git` today, and the owner's unsandboxed git reads it:
  the owner's terminal, editor, git apps, and the coding agents' own git run outside their sandboxes. The fence
  (`enforce.git: platform`, slices a–c, steps 3–6) is what makes those safe. Platform push doesn't need it: it never
  runs the owner's git in the project's repo.
- **Commits, config and brunna's own flows through the platform** (step 5: memory sync, `merge-branch`, `with-lock.sh`).
- **Network reads for sessions:** slice d's fetch, pull, clone and other-URL fetch.
- **The session socket** could later carry push requests for a synchronous answer. It isn't needed for "sessions only".

**The broker's order of work, revised** (in `git-broker.md`, as an appended section; nothing above it is deleted):
1. Platform push (this doc). It needs nothing from slices b–d and goes first.
2. Slices b and c, then slice d without its push half (fetch, pull, clone, other URLs).
3. Steps 3–8 as before. Step 5's `merge-branch` push is the request above.

## 8. Residuals

- **Publication and content the checks can't judge (B3).**
  - A push is a way out of the sandbox for data: a colleague's document committed to the repo leaves with it. On a
    public remote that is publication, whatever the branch. No pattern tells a document from code.
  - What bounds it:
    - the remote's visibility;
    - `none` as the default for a public remote;
    - check 8;
    - `branches` only for what the forge does with the default branch.
  - For a project that holds other people's data, keep the remote private.
- **Branches that deploy or publish themselves.** `gh-pages`, a hosting service's production branch set to another
  branch, and preview deploys of every branch build or publish what `others` lets through, sometimes with the deploy's
  secrets. The platform doesn't read forge or hosting settings. On a remote with such a setup, `none` or the owner form
  is the owner's call.
- **CI that already exists runs the pushed code** with the repo's secrets (`git-broker.md`, owner-run push, "What the
  gate doesn't stop"). Check 5 keeps the CI files fixed, not what they execute.
- **The secret scan's limits** (check 7).
- **Sessions that letters drive (S4).** A woken session, or one acting on a letter, is a session and can request a push:
  anything that can mail the project can ask it to. The mailbox's background branches and headless agent can't (their
  settings are the platform's, without the folder). So can code the project wrote that a session runs (r2 S-a): a
  job's planted git hook, fsmonitor or Makefile runs `brunna push` inside a session, which the git fence would stop.
  Mitigations for later:
  - hold a request made within a turn that a wake started, and ask the owner (the hooks know which turn a wake began);
  - name the letter's sender in the push record;
  - the rules text says letters are data, as it does now.
- **No rewrite of any branch.** An agent that rebased a branch it had already pushed pushes it under a new name.
  Rewriting only branches whose remote tip is the platform's own last push for this project would be safe (it's in
  `pushes.jsonl`), and is left for later.
- **Latency:** up to a minute for dispatch, plus the push. The client's 90 s covers a small push; a larger one answers
  by letter.
- **The owner's credential** while open question 1 stands:
  - its reach is the whole account, bounded only by the checks and `paths.origin`;
  - a Keychain-stored credential fails while the screen is locked and becomes an owner to-do.
- **Not covered:**
  - the first push to an empty remote (`upgrade-idea` and onboarding make it);
  - tags and releases;
  - Git LFS;
  - forge actions such as pull requests and issues (`gh` stays out of the sandbox);
  - remotes other than `paths.origin`.
- **Linux:**
  - the bundling job runs under Landlock (the owner form's end-to-end test already runs in `bin/linux-test`);
  - the session request folder needs no carve-out there (S4);
  - dispatch is a systemd user service, which usually has no ssh agent. The probe says so;
  - the fence (`enforce.git`) is still refused on Linux, which doesn't affect this.

## 9. Build order and tests

**Build order:**
1. `registry.validate_enforce` for `git_push`, and the `brunna.json` settings (schema).
2. `git_remote.py`:
   - split `push` into `prepare` (registry, remote, bundle, staging, plan) and `send`;
   - `push_credential`;
   - `bin/push_checks.py` (checks 4–9, blob-based);
   - the owner form's flags and override rule (S1);
   - `serve <project>`;
   - `setup`.
3. `policy.session_write` and `push_dir`; the adapters' session renderings (Claude Code, Codex), and nothing in
   `sandboxed-run` or `settings: platform` runs; `SESSION_SUBCOMMANDS`. The rendering report is checked.
4. `hubbus`: `take_push_requests`, the queue and its idempotence, the worker's start and restart, the result file.
   The client in the `brunna push` dispatcher, with the forms.
5. The rules text, `docs/commands.md`, doctor and status-page rows (per sandboxed project: `git_push`, the last push,
   the scanner), and the daily probe in `brunna/secrets`.
6. Onboarding: `connect` (and its flags), `onboard-project.sh` flags, the skill's decision 5, `upgrade-idea --push`,
   the lease-runner default, `connect_project`'s `--push none`, and the setup to-do.
7. `merge-branch`'s request on `push_on_merge` in a sandboxed project.

**Tests** (`tests/test_platform_push.py`, on top of `tests/test_git_remote.py`'s remote, http-backend and recording
credential helper). Every refusal comes with a plain-git control that pushes the same thing:
- **Who may request (S4):**
  - a session's sandbox (the rendered Claude Code settings through the conformance harness, and Codex's) can write its
    own request folder and not another project's;
  - a service and a command job through the real `sandboxed-run` (Seatbelt on macOS, Landlock in `bin/linux-test`)
    can't write it, with the control that they can write the workspace;
  - a `settings: platform` run's rendered settings don't carry it;
  - a project named in the file is ignored.
- **Taking in (S2):**
  - a malformed request gets a result file, or a letter when its id can't be read;
  - a second request while one is queued is refused;
  - the same id taken in twice does nothing the second time;
  - the daily cap holds;
  - the worker starts detached and outlives the dispatch run;
  - a worker killed mid-push is replaced on the next tick, and an old request with no worker ends as `worker lost`.
- **The lock (S10):** a request taken in while the owner form holds the lock waits and then lands.
- **Each check of section 4, with its fail-closed case:**
  - B1: a secret in a merge commit's own blob; one behind a NUL in the first 8 KB; one added and then removed. Each is
    refused, with the control that `git log -p` shows none of them;
  - B2: a `gitleaks:allow` line is still found; a `.gitleaksignore` and `.gitleaks.toml` in the project change nothing;
    with gitleaks installed, a new `.env` is still refused by the built-in set; a gitleaks that exits 3, or doesn't
    know `stdin`, refuses;
  - S3: a declared secret at an absolute path, at `~/…`, or as a link to a file outside is neither read nor reported to
    the project;
  - an LFS pointer; the names list named but missing; the anonymous probe failing (treated as public); the owner's home
    path on a public remote; an empty emails list (owner to-do);
  - S9: a refused push leaves no object in the owned repo; an oversized bundle stops at its cap; size refuses before
    any scan runs.
- **The sha pin:** the branch moved after the request, refused.
- **The field:** `none`, `others` against the default branch, `all`. Validation refuses each bad shape, and a
  `manifest.update` that sets `git_push` is refused.
- **The owner form (S1):**
  - every check's flag is shown;
  - `--yes` with a failed check refuses;
  - an interactive "y" pushes past it;
  - run inside a sandboxed session (real `sandboxed-run`), it refuses before contacting the remote;
  - `setup` is never a project.
- **The reply:**
  - the result file is written through no link (a planted symlink at the result's name is replaced, not followed);
  - the letter is `info`;
  - a remote's raw refusal text (a canary in the http-backend's message) never reaches the result file or the letter,
    only `pushes.jsonl` and the to-do (S8);
  - an owner-fixable refusal raises one to-do per episode, and the "branch not allowed" one closes when an owner push
    lands that sha.
- **Isolation:**
  - the worker's git runs only in owned or staging repos with `--git-dir` (recorded at `run_git`, as built);
  - a canary in the project's `.git` never runs;
  - no scanner reads a file from the workspace.
- **End to end:** a session's `brunna push` in a throwaway instance, through the rendered session sandbox and the real
  dispatch.

A blind review of this design before building (done: r1 above), and of the code before `merge-branch` (the working
rule).

## Open questions for the owner

1. **Which credential should the platform push with?**
   - **The owner's,** as the base above: no setup, and the same as `mirror-backup.sh` and `upgrade-idea.py` use today.
     Its reach is the whole account. It works unattended only if it is file-stored or an ssh key; a Keychain-stored one
     fails while the screen is locked.
   - **A per-repo push grant held by the platform**, in the style of `platform-credentials.md`: a write deploy key in
     the platform's secrets directory, its host key pinned, no agent and no Keychain. Its reach is one repo, and it
     works with the screen locked. It costs a grant step:
     - at onboarding, `gh repo deploy-key add --allow-write` with the owner's `gh`, done by the script;
     - once for each of the five existing projects;
     - other forges: the owner adds the printed key once.

   **Recommended:** the per-repo grant ("If the grant" below), built as step 2's `push_credential`, before the first
   project is switched to `all`. A wrong push can then reach one repo only, and a new user's default `gh` setup
   (Keychain) doesn't break pushes at night.

   **If the grant** (what changes; everything else in this doc stays):
   - **What and where (nit):** one ed25519 key per project, made by the platform, in its own folder,
     `<secrets>/git-push/<project>/`:
     - `key` (600);
     - `grant.json`, holding the push URL, the host and the pinned host key.

     It is outside `<secrets>/credentials/`, whose `*/*.json` files `credentials.py` walks as its own grants. Every
     sandbox denies the secrets directory (`policy.secret_dirs`).
   - **Declared by derivation, not by a `secrets:` entry:** a project whose `enforce.git_push.branches` isn't `none` and
     whose origin isn't a local path has the grant `git-push`. It is owner-only because `enforce` is, and
     `check-secrets.py` and doctor derive it as they derive the alert and provider keys. It is never in the cloud
     backup: re-granting is cheap, so a restore raises the to-do.
   - **How it pushes:**
     - `GIT_SSH_COMMAND` is `ssh -F /dev/null -i <key> -o IdentitiesOnly=yes -o IdentityAgent=none
       -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<the pinned key>`;
     - `GIT_CONFIG_GLOBAL` is an empty platform file and `GIT_CONFIG_NOSYSTEM=1`, so no credential helper or
       `insteadOf` of the owner's is read;
     - no Keychain and no agent are involved, so it works with the screen locked and on Linux.
   - **The push URL:**
     - `paths.origin` itself when it is ssh (`host:path`, `ssh://`);
     - for `https://<host>/<path>`, the mechanical `git@<host>:<path>`, shown at the grant and recorded;
     - a mismatch with `paths.origin`'s host and path afterwards (the owner changed the origin) breaks the grant and
       raises a to-do, with no guessing.
   - **Who adds the key:**
     - `brunna push setup` (owner-run), `onboard-project.sh` and `upgrade-idea.py`, which already create the repo with
       the owner's `gh`: `gh repo deploy-key add --allow-write --title "brunna push"`;
     - on GitHub, the host key is pinned from `gh api meta`'s `ssh_keys`; on other hosts, from the owner's
       `known_hosts` (`ssh-keygen -F`);
     - a forge without `gh` support: setup prints the public key and its fingerprint, and the owner adds it once. That
       is a person, because no rule can do it;
     - **a forge or organisation that disables deploy keys (nit):** no platform push for that repo. The setup and the
       probe say so, and nothing falls back to the owner's credential.
   - **The probe and the to-do:**
     - the daily probe is `ls-remote` through the grant;
     - "Permission denied (publickey)" or a changed host key breaks it: an owner to-do with `brunna push setup
       <project>`, one per episode (`raise_todo`);
     - a revoke, `brunna push setup <project> --revoke`, deletes the key at the forge and here.
   - **Build order:** added to step 2 (`push_credential`, `setup`) and step 6 (onboarding, `upgrade-idea`). Its tests
     use an sshd on 127.0.0.1 with the test key, plus a control in which the owner's agent key is the only one that
     would work, which must fail.
2. **What should onboarding set, with no person answering, for a sandboxed project whose remote is public (or whose
   visibility can't be told)?**
   - `none` plus a one-time owner to-do to run `brunna push setup`;
   - `others`;
   - `all`.

   **Recommended:** `none` plus the to-do. On a public remote every allowed branch is published as it lands, and
   `others` doesn't change that (B3). Deciding once is the owner's look at publication, not a push.
3. **Who may ask the platform to push?**
   - only the project's sessions, enforced by a request folder that only their sandboxes can write;
   - any process of the project (its services and jobs too), through the outbox.

   **Recommended:** only sessions, as r3 decided. The folder makes it a rule on both OSes with no carve-out (S4). The
   sessions that letters drive stay a residual, with the mitigations in section 8.

### The owner's answers (11-15)

The owner took all three recommendations:
1. **Credential:** a per-repo write deploy key held by the platform ("If the grant"), added by `gh` at onboarding and
   once for each existing project with a remote, before any project gets `branches: all`. No fallback to the owner's
   own credential.
2. **Onboarding with nobody answering, public or unknown remote:** `none` plus a one-time `brunna push setup` to-do.
3. **Who may request:** only the project's sessions, through the session request folder.

### Built: the grant and onboarding's questions (11-16)

- **`bin/git_push_grant.py`**, `brunna push setup <project> [--branches …] [--email …] [--yes] [--grant-only]` and
  `--revoke`. The key and `grant.json` are in `<secrets>/git-push/<project>/` (700/600). The deploy key's title is
  `brunna push: <project>`; a rerun with a working grant keeps it, and one that stopped halfway replaces its own titled
  key at GitHub. The grant is checked with `ls-remote` through the key alone; a key that doesn't work is deleted again.
  `grant.json` is written last. An existing grant is made again only when GitHub refuses the key ("Permission denied
  (publickey)"); a timeout or a changed host key fails and keeps it. A grant for an origin that changed has its old
  repo's key deleted first. `--revoke` deletes every key with the project's title on the record's repo and the
  registry origin's, so a lost record leaves none behind. While the registry doesn't know `enforce.git_push`, setup
  makes no grant and the to-do stays open (blind code review, 11-16).
- **Where the build differs from "If the grant":** GitHub's host keys are checked in (`bin/known_hosts/github.com`,
  matched against `api.github.com/meta` and its published fingerprints, a test pins them), not read from `gh api meta`
  at setup. Any forge other than GitHub, an ssh alias and a local path are refused with the reason; setup doesn't print
  a key for another forge to add by hand. `gh` warns that deploy keys it added go when its token is de-authorised:
  the daily probe in `brunna/secrets` catches that and raises the setup to-do.
- **The push core's interface:** `ssh_env(project, run_dir=None)` is an overlay that must win over `owner_env()`
  (`GIT_SSH_COMMAND` with `-F /dev/null -o IdentityFile=none -i <copy> -o IdentitiesOnly=yes -o IdentityAgent=none`;
  `IdentitiesOnly` alone doesn't stop ssh's default `~/.ssh/id_*` when the `-i` file is gone, `IdentityFile=none`
  does. `<copy>` is the key copied into the run's private folder (`run_dir`, which the caller removes; without one, a
  folder removed at process exit; `push_env(project)` is the context-manager form), checked against the grant; the
  pinned known_hosts,
  `StrictHostKeyChecking=yes`, `BatchMode=yes`; an empty platform `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM=1`).
  `push_url(project)` is `git@github.com:<owner>/<repo>.git`, also for an https origin. Both raise `NotSetUp(reason)`.
  `status(project)` gives the doctor row, and `probe(project)` the daily check.
- **The policy** goes into `enforce.git_push` only after the grant works, and only when the registry knows the field
  (checked at run time); until then setup says what to set. The project repo's `user.name`/`user.email` are then set,
  as the project (`project-git-job.py set-identity`), to the allowed address (the noreply one when it is allowed), so a
  public remote's sessions don't commit with the global email and get every push refused. The rules text says so.
- **The registry entry is edited as text:** `write_policy` replaces or adds only the `git_push:` block under
  `enforce:` and leaves every other byte, so the nested and inline comments that record the owner's decisions stay;
  the result must read back as exactly the entry intended, or nothing is written. **Known issue:** `upgrade-idea.py`
  still rewrites the whole entry with `yaml.safe_dump` and keeps only the lines that start with `#`, so an upgraded
  idea loses its nested and inline comments.
- **Onboarding:** `onboard-project.sh` and `connect.py` take `--sandbox`/`--no-sandbox`, `--push`, `--push-email`, and
  connect asks both questions. `connect_project` may ask for `sandbox` and always passes `--push none`.
  `upgrade-idea.py --push`; the lease-runner passes `none`. With nobody answering, the anonymous probe decides
  (private: `all`; public or unknown: `none` plus the one-time to-do). The to-do is also raised when `none` came from
  an agent (connect_project, the lease). A grant that fails at onboarding leaves `none`, and the to-do says why.
- **Not built here:** the design's sshd end-to-end test; the anonymous probe also sits in `git_push_grant.visibility`,
  so check 8 should share it rather than make a second one. `rename-project.sh` doesn't move a grant's folder: after a
  rename, the next probe raises the setup to-do.
