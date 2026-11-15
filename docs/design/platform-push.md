# Platform push: a sandboxed project's agent says "push", the platform pushes

Status: design, 11-15, not built. It builds on `brunna push` (`bin/git_remote.py`; `git-broker.md`, "Owner-run push,
built first (11-15)") and takes over the push half of the git broker's slice d (section 7). Open questions for the
owner at the end.

**The owner's principle (11-15):** no person, neither the owner nor anyone else who runs brunna, should have to run
`git push`. A project's agent says "push" and the platform does it. People appear only when a rule can't be met.
**And (11-14):** a user never has to remember a special way to use their agent. Using their own agent tool as usual
should give them all of brunna. brunna is open source, so everything here has to work for other users and other forges
too.

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
2. **If sandboxed, may its agent push the default branch by itself?** "The platform pushes for it after the automatic
   checks in section 4. Other branches are always allowed. If you say no, a push to the default branch waits for you."

Answers go into the registry as owner-only fields (section 5):
- **`brunna connect`** (`bin/connect.py`) asks both questions per project, in its existing UI, before it calls
  `onboard-project.sh`.
- **`onboard-project.sh`** stays non-interactive and gains flags:
  - `--sandbox` / `--no-sandbox`;
  - `--push none|others|all`;
  - `--push-email <address>` (repeatable).

  It writes the answers through `ONBOARD_REGISTRY_EXTRA`, as `create-idea-sandbox.py` does.
- **The `new-or-onboard-project` skill** gains a "Decision 5: sandbox and push". It asks the owner in the
  conversation, with the criterion, and passes the flags. Like any onboarding the skill runs, the run is in the owner's
  own session, where the owner answers.
- **`create-idea-sandbox.py`:** no change. An idea is always sandboxed, and with no remote its push stays `none`.
- **`upgrade-idea.py`** gains `--push none|others|all`. It already creates the GitHub repo with the owner's `gh` and
  makes the first push, so it is where the push field is set (default below). The lease-runner endpoint
  (`POST /v1/sandboxes/<name>/upgrade`) passes it on.
- **The MCP server's `connect_project`**, which an agent calls, may set `sandbox: true`, since that only narrows. It
  never sets `git_push` above `none`. What it leaves is an owner to-do with the one command that sets it (section 6's
  `brunna push setup`).

**Non-interactive defaults** (flags absent, or `--yes`):
- sandbox: **off**, as today. A script can't judge the criterion, and nothing changes for anyone who doesn't ask;
- push for a sandboxed project with a remote: **`all`** for a private remote and **`others`** for a public one (open
  question 2). Publishing the default branch of a public repo is the one push the checks can't judge (section 8);
- push with no remote: `none` (the registry refuses anything else);
- emails: section 4, check 6.

## 3. The platform pushes on the project's request

### The command

In a sandboxed project's folder, a session runs:

    brunna push [<branch>] [--no-wait | --wait <seconds>]

**The request.** `brunna push` resolves the branch (the one checked out in the worktree it runs in, unless named) and
that branch's commit with git's read-only commands. It then writes one file into the project's outbox,
`<workspace>/.git/brunna/outbox/`, through `hubbus.outbox_put`:

    {"v": 1, "op": "push", "id": "<outbox id>", "branch": "<name>", "sha": "<40 or 64 hex>", "session": "<id>"?}

Only `branch` and `sha` are read as the request. The project, as in every outbox file, is the outbox's own and never
read from the file (`bus-sender.md`). The `session` is kept only when `hubbus.own_session` confirms it, and only to
address the reply. Nothing in the file widens what the registry allows: no flag, no remote, no destination ref.

**The forms of `brunna push`,** decided in this order:
1. **`brunna push setup <project> …`:** the owner's setup (section 6). `setup` is a reserved word, never read as a
   project.
2. **`brunna push --owner <project> [<branch>] [--yes]`:** the owner form (today's), from anywhere.
3. **The cwd is inside a registered project's workspace or runtime folder** (found by folder, as `hub-run`'s
   `find_workspace` does):
   - sandboxed: **the request form**, and the one positional argument is a branch;
   - unsandboxed: "push with git as usual", exit 2.
4. **Anywhere else:** the owner form, `brunna push <project> [<branch>] [--yes]`, as today.

**The owner form doesn't work from a sandboxed session**, though the permission rule lets `brunna push` through: the
rule only skips the prompt, and the command still runs inside the session's sandbox. It has to make and write
`<state>/git-broker/<project>/`, which no sandbox can write (`policy.git_broker_dir`). So the owner form first checks that
it can write there, and otherwise refuses before contacting anything: "run it in your own terminal, outside the
project's sandbox". The checks the owner form skips (6–9) are skipped only for the owner, who sees every commit.

**Permissions.** `push` joins `policy.SESSION_SUBCOMMANDS` and `agenthub`'s `SESSION_COMMANDS`, so the permission rules
let `brunna push` and `agenthub push` through. This changes every sandboxed project's rendered settings once. It is
checked with `render-project-settings.py --report` before the merge, and nothing else in the renderings may change.

**Waiting.**
- The client waits up to `--wait` (default 600 s) for its result file, `<workspace>/.git/brunna/push/<id>.json`. The
  platform writes that file with `nofollow.replace`, as it writes the mailbox view.
- It prints the result and exits:
  - 0: pushed, or nothing to push;
  - 2: refused;
  - 1: failed;
  - 75: no answer yet. The answer then comes as a letter.
- `--no-wait` exits 75 at once.

### Who runs it, and why there

**Taking the request in.** `hubbus dispatch` (service `local.brunna-mailbox-dispatch`, every minute, as the owner)
already reads every outbox in `take_outboxes`. `take` gains the op `push`:
1. It checks the shape: `branch` text, `sha` hex, the id in the outbox id form.
2. It applies the limits below.
3. It moves the request into the platform's own folder, `<state>/git-broker/<project>/requests/<id>.json`
   (`policy.git_broker_dir`). No sandbox can write there.
4. It starts the worker, detached, unless one is already running for the project.

A malformed request is refused through the existing `outbox_refused`, which tells the project in an `info` letter. A
push request is not a letter: it never enters the bus's message table, so no mailbox handler, wake or round limit is
involved.

**The worker:** `git_remote.py serve <project>`.
- It runs as the owner, outside every sandbox. It takes `git_remote.locked()` for the project and serves its one
  queued request.
- It is started like the mailbox's background runs (`subprocess.Popen(..., start_new_session=True)`,
  `bin/hubbus` near line 2565), with the double fork and `setsid` that `run-agent-job.sh` uses. A plain child of the
  dispatch tick dies when launchd ends that tick's process group (`known-issues.md`, "when a launchd job ends").
- A push can take up to `git_remote.TIMEOUT_S` (30 min), so it can't run inside the tick, which has to stay quick for
  mail.

**Why not the alternatives:**
- **An event job:** the scheduler ticks every 15 minutes, which is too slow for an agent waiting on "push". It would
  also need a new bus event type for a request, plus cursor state for what is a one-shot command. The bus is a log of
  claims, and nothing here needs it: the outbox already binds the sender.
- **cap-runner:** it is in the optional `sandbox` module, so sandboxed projects on an install without it couldn't push.
  Its request folder, `.hub-run/`, is in the workspace, so it is no harder to forge than the outbox. Its jobs are runs
  under the platform's Seatbelt profile or model calls, and a push is neither.
- **The dispatch tick itself:** see above. It must not block mail.

### The pipeline

The worker runs the same steps as `brunna push --yes`: `git_remote.push` with the owner's prompt replaced by the checks
in section 4. In order:
1. Check the registry: `isolation.sandboxed`, `check_origin(paths.origin)`, and `enforce.git_push` (section 5) allows
   this branch. `valid_branch` checks the name.
2. Take `remote_heads` and `fetch_remote` in the owned bare repo. A failure here says what it is: the default branch
   can't be told, the remote is empty, or the remote can't be reached.
3. Run `bundle_in` as the project (`project_git.job` `bundle-branch`, sandboxed), then git's object checks in the owned
   repo. **The bundled tip must equal the request's `sha`.** If the branch moved after the request, the push is
   refused ("ask again").
4. Fast-forward only, against the remote's ref, and run every check in section 4.
5. Push with `--force-with-lease=refs/heads/<branch>:<old>`, the exact sha. This is `git_remote`'s step 6 unchanged.
6. Log a line in `pushes.jsonl` with `requested_by` (the confirmed session, else `"project"`) and `checks` (each check's
   result). Write the result file, and send the letter.

**The credential step** is one function, `push_credential(project) -> env`, and nothing else depends on which
credential it gives.
- Until the owner answers open question 1, it is what `brunna push` uses: the owner's git environment (`KEEP_ENV`) as
  the dispatch service has it. That is the same credential `mirror-backup.sh` fetches with every night and
  `upgrade-idea.py` pushes with.
- Under launchd or systemd that environment is not a terminal's. A Keychain-stored helper (`osxkeychain`, or `gh`'s
  default storage) can't be read over SSH or while the screen is locked (`known-issues.md`, "claude under SSH is 'not
  logged in'"). A file-stored `gh` token (`gh auth setup-git`) or an ssh key works.
- **Doctor and a daily probe** (part of `brunna/secrets`) run `ls-remote` exactly as the worker would, from the service
  environment, for each project with `git_push` set.
- A failure is an owner to-do, one per episode (the `credentials.py` pattern: `owner_notify`, `raise_todo`). There is
  never a fallback to another credential.

### Who may request

**The boundary is the project, not the kind of process.** Anything that runs as the project can write its outbox
(`bus-sender.md`, "Left as it was"): sessions, services, command jobs, a confined run. No file channel can tell them
apart, and this design doesn't claim it can.
- A confined run (`names.env("CONFINED")`, set by `llm.py`) and an agent the platform sends letters for (`propose_apply.py`) are refused in
  the client, as their mail is. That check is in the client, so it guides the run but is not a boundary.
- "Sessions only" becomes enforceable once the broker's session socket exists (section 7). It could then be an owner
  field (open question 3).
- What a request can do is bounded the same way whoever writes it: the owner's field, the checks, one remote, one
  repo.

### Limits

- **One in flight per project, and a queue of one.** A request while another is queued or running is refused: "one
  push at a time; wait for <id>". So the worker serves at most one request per project. The owner-run form shares
  `git_remote.locked()`: a request taken in while the owner pushes waits for that push to finish.
- **Per project per day:** `git_push.requests_per_day` in `brunna.json` (default 20), refusals included, counted in the
  platform's folder.
- **Across projects:** `git_push.concurrency` (default 2) workers at once. Others wait in their queues.
- **A request older than 10 minutes** when the worker reaches it (the machine slept) is still served. The sha pin
  keeps it exact.

### The reply

**The result file** (read by the waiting client):

    {"v": 1, "id", "state": "pushed|nothing|refused|failed", "branch", "old", "new", "commits",
     "check": "<the check that refused, or null>", "reason": "<plain text>", "fix": "project|owner", "todo": "<key>"?}

**Every outcome with a valid id writes the result file:** a refusal at take-in (one in flight, the daily cap) as much
as a push. A waiting client never sits out its whole wait for a refusal. Only a file whose id can't be read gets just
`outbox_refused`'s letter.

**The letter:** from brunna to the project, type **`info`**. The text is the outcome in one line, plus the reason and
what to do. It goes to the project, not to a session: `hubbus` addresses a session only through a reply to a letter that
session sent, and a request is no letter. The requesting session hears it through its client. The letter is for
everyone else, and for the requester once its wait has ended. It is a notice, never a `request`:
- a `request` would start mailbox handlers, wake sessions and count against the round limits, all for the project's
  own action;
- the requester sees the outcome in the client, or at its next session through the hooks. If nobody is open, the
  letter waits in the inbox like any other.

**Refusals the project can fix** say how (`fix: project`), and nothing more happens:
- not a fast-forward ("merge or rebase onto origin/<branch>");
- the branch moved since the request;
- an author or committer email not allowed (check 6: "re-author the unpushed commits");
- a secret or private-name finding (checks 7 and 8: file, commit and rule, never the value);
- size (check 9);
- a CI path changed without `git_ci` (check 5);
- a daily limit reached.

**Failures only the owner can fix** become an owner to-do (`fix: owner`). The to-do is sent with `owner_notify`, one per
cause and project per episode, and its key is in the result. It is closed when that cause next passes:
- the field doesn't allow the branch (`none`, or the default branch under `others`). The to-do names the range and
  `brunna push <project> <branch>`, which pushes it after the owner looks;
- the credential can't reach the remote (auth, an unknown host key);
- the remote is empty (its first push isn't this command's);
- the remote's own rules refused it: a protected branch, or GitHub's email privacy (GH007). GitHub's secret push
  protection (GH013) is the project's to fix instead;
- `paths.origin` is missing or refused;
- three failures in a row the platform can't classify.

A project that thinks a refusal is wrong writes to the owner (`brunna send --to owner --type question`). The owner-run
`brunna push` is still the way to push past a check: the owner sees everything and says yes.

## 4. The checks that replace the owner's look

Each check fails closed: when it can't run, or can't tell, the push is refused and the reason names the check. All of
them run in the owned bare repo, on the commits `<new> --not <remote tips>`: **every** new commit, not the 50 that
`plan()` shows.

1. **Origin:** `check_origin` as built (`paths.origin` only; no ext/fd, no local path a sandbox can write, no temp
   area).
2. **Ref shape:** one branch, no tags, no deletes, no refspec, no force (`branch_arg` as built). The branch is allowed by
   `enforce.git_push.branches`.
3. **Fast-forward and pin:** `old` is an ancestor of `new`. The lease pins `old`, and the bundled tip equals the
   request's sha.
4. **Objects:** git's fsck through the staging repo (`hasDotgit` an error), as built. A new gitlink (mode 160000) is
   refused, since there are no submodules (r3 question 5).
5. **CI files:**
   - The pushed commit's `.github` entry must equal the remote default branch's (`github_entry`, as built), unless
     `enforce.git_ci: true`.
   - The platform push also compares, the same way, the other forges' CI entries at the root: `.gitlab-ci.yml`,
     `.circleci`, `.buildkite`, `.travis.yml`, `Jenkinsfile`, `azure-pipelines.yml`, `bitbucket-pipelines.yml`,
     `.gitea`, `.forgejo`, `.woodpecker`, `.woodpecker.yml`.
   - Nobody reviews these, so the list errs wide. The owner-run form only flags them.
6. **Identity:**
   - Every new commit's author email (`%ae`) and committer email (`%ce`) must be in `enforce.git_push.emails`.
   - An empty or missing list refuses every platform push ("the owner hasn't listed the emails this project commits
     with").
   - The default is proposed at onboarding and written into the field, never computed at push time:
     - the owner's `git config --global user.email`, read by onboarding in the owner's own process;
     - on GitHub, when the owner's `gh api user` answers, also `<id>+<login>@users.noreply.github.com`;
     - on other forges, the global email only, and the owner adds that forge's private address if they use one.
7. **Secrets in the pushed history:**
   - **Input:** the patch of every new commit, each commit's own diff, since history is published even when a later
     commit removes the secret: `log -p --no-ext-diff --no-textconv --no-renames <new> --not <tips>` from the owned
     repo. The scanner never reads the project's folder, so the project's `.gitleaks.toml`, `.gitleaksignore` or
     `.gitattributes` can't steer it.
   - **gitleaks when installed:** `gitleaks stdin --redact --no-banner` with its default rules and the platform's own
     config path, never one from the repo. Version 8.19 or newer, for `stdin` (unmeasured). A finding refuses. Any
     other exit, or a timeout, refuses as "the scanner failed", never as a pass.
   - **Without gitleaks, a built-in set** (`bin/push_checks.py`, high-confidence only, on added lines):
     - private key blocks (`-----BEGIN … PRIVATE KEY-----`);
     - AWS key ids (`AKIA`/`ASIA` + 16);
     - GitHub tokens (`gh[pousr]_…`, `github_pat_…`);
     - Slack (`xox[abprs]-…`), Google API keys (`AIza…`), Stripe live keys (`sk_live_`, `rk_live_`), Anthropic
       (`sk-ant-`), OpenAI (`sk-proj-`);
     - new files named `.env` or `.env.*` (not `*.example`, `*.sample` or `*.template`), `*.pem`, `*.p12`, `*.pfx`,
       `*.key`, `id_rsa*`, `id_ed25519*`.
   - **Both, always:** the exact values of the project's own declared secrets (registry `secrets:`; a whole file, or an
     `env_key`'s value), read by the worker as the owner. Those paths are in the project's folders, which the project
     can write. So each one is read with `nofollow.read`: no link followed, regular files only, at most 64 KiB. A link
     or anything else there is skipped and named in the result. Otherwise a link there could turn the refusal into an
     oracle on files the sandbox can't read. Only values of 12 characters or more count, and matches are reported by
     name, never by value.
   - **What it can't catch:**
     - secrets that are encoded, split, encrypted, compressed or inside binary files;
     - formats outside the rules;
     - a secret the project made up that isn't declared;
     - personal data, which has no pattern (names, addresses, a colleague's document): see section 8.
8. **Private names (public remotes):**
   - When the remote is public, no commit message, author or committer name, changed path or added line may name a
     project from the instance's `private-names.txt` (`private_names.found`), other than this project's own name.
   - **Public** means an anonymous `ls-remote` of the origin's https form works: no credential helper,
     `GIT_TERMINAL_PROMPT=0`, a throwaway HOME. The https form is the URL itself, or the mechanical one for `host:path`
     and `ssh://`. A local path counts as private.
   - **Can't tell** (no https form, or the network failed): treated as public. The cost is a false refusal, never a
     leak.
   - The registry records no visibility (`onboard-project.sh` step 11 writes none). The live probe stays right when
     the owner flips a repo's visibility.
   - A list that is named but unreadable (`private_names.ListError`) refuses. A deployment with no list skips the
     check.
9. **Size:**
   - at most `git_push.max_commits` new commits (`brunna.json`, default 500);
   - each new blob at most `enforce.git_push.max_file_mb` (default 10, at most 100, under GitHub's 100 MB refusal);
   - all new blobs together at most `git_push.max_total_mb` (default 200);
   - the bundle at most `project_git.MAX_BUNDLE`, as built.

   Measured with `rev-list --objects` and `cat-file --batch-check`. Binary or not doesn't matter: size does.

Not a check, on purpose: **the project's tests.** `brunna test`'s recorded passes live in the project's git dir, which
the project can write, and its tests are its own code. They show quality, not safety, and `merge-branch` already runs
them before anything lands on main.

## 5. Registry fields

Owner-only, under `enforce` (already in `PLATFORM_OWNED`, so a `manifest.update` that touches them is refused whole):

    enforce:
      sandbox: true
      git_ci: false                      # as built
      git_push:
        branches: all                    # none | others | all
        emails: [me@example.org, 1234+me@users.noreply.github.com]
        max_file_mb: 10                  # optional, 1–100

- **`branches`:**
  - `none`: the platform pushes nothing. This is today's behaviour, and an absent `git_push` means the same;
  - `others`: any branch on `paths.origin` but the default (the remote's `HEAD`, and the registry's branch when it
    names one);
  - `all`: the default branch too.
- **Validation** (`registry.validate_enforce`), for anything but `none`:
  - needs `enforce.sandbox: true`;
  - needs a `paths.origin` that `check_origin`'s syntax accepts;
  - the project may not be in `service_sandbox_exempt` (its jobs, the bundler's included, run unsandboxed: the owner
    form refuses those too);
  - `emails` must be a non-empty list of `local@domain`, one line each, printable;
  - `max_file_mb` must be an integer from 1 to 100;
  - unknown keys are refused.
- **The 11-15 decision "sessions push only non-default branches"** (r3 question 2, slice d) was one rule for every
  project. It is replaced by this field: the owner decides per project, and `others` is that old rule.
  - The other parts of that decision stay: no tags, no `.github/**` without `git_ci`, no signing.
  - Its `--force-with-lease` on own branches does not: this push is fast-forward only (section 8).
- **`push_on_merge`** stays the project's own field. In a sandboxed project, `merge-branch`'s step 5 writes a push
  request for the default branch instead of running `git push`, which can't reach a credential there anyway. Whether
  that request lands is the owner's `git_push.branches`.
  - Slice d's rule that `validate_enforce` refuses `push_on_merge` with `enforce.git: platform` is no longer needed. It
    existed because the broker's own push would have obeyed `push_on_merge`.
- **Machine-wide limits** go in `brunna.json` and `contracts/hub.v1.schema.json`, as `git_broker.*` do:
  `git_push: {requests_per_day, concurrency, max_commits, max_total_mb, wait_s}`.

## 6. Existing projects

- **Nothing changes until the owner sets the field.** An existing sandboxed project has no `git_push`, so it behaves as
  `none`:
  - a `brunna push` request from it is refused with an owner to-do that names `brunna push <project> <branch>`;
  - the owner-run form works as today.
- **`brunna push setup <project> [--branches others|all] [--email <address>…]`** is owner-run, in a terminal or the
  brunna session:
  1. it proposes the emails: the owner's identity, the GitHub noreply address, and the remote default branch's recent
     author emails, shown for the owner to pick;
  2. it runs the probe of section 3, so a credential that can't reach the remote shows up now and not at the first
     request;
  3. it writes the field with `commit-hub-files.sh`, as every script-written registry change.
- **The owner's decision for this deployment (11-15):**
  - All six existing sandboxed projects keep the sandbox. Each meets section 1's criterion: other people's data, or
    downloaded or external content run or processed unattended. One is kept as the sandbox's test bed.
  - The five with a remote get `branches: all` once this lands.
  - The sixth is still an idea with no remote and has nothing to push (`none`; validation refuses more).
  - None of the five has a CI workflow today (no `.github/workflows`), so `git_ci` stays unset.
  - Their names are in the instance's registry only, never here.
- **The rules text** (`templates/agent-instructions.md`): the line "In a sandboxed project pushing is {{owner}}'s
  alone: … ask {{owner}} to run `brunna push <project> <branch>`" becomes "In a sandboxed project, push with
  `brunna push [<branch>]`, never `git push`: the platform checks and pushes it, and tells you why when it doesn't."
  - `sync-local-setup` renders it at each project's next session.
  - `templates/rules/idea-sandbox.md` keeps "no remote, no push" for ideas.

## 7. Relation to the git broker

**What this supersedes:**
- **Slice d's session push:**
  - the session socket's `push`, "sessions only", "the project's own non-default branches", `--force-with-lease` there;
  - slice d's step 4 for pushes (P1 updates the remote-tracking ref). The project's `origin/<branch>` still catches up
    at its own next fetch.

  The request comes through the outbox, the policy is the owner's per-project field, and the mechanics are
  `git_remote.py`'s, already built and tested.
- **`idea-sandboxes.md`'s "Pushing from an upgraded sandboxed project … waits on the git broker"** is answered here.
  "Only main goes to the public remote" becomes `branches` plus check 8.

**What the broker still gives:**
- **The fence.** A sandboxed project's processes can write its `.git` today, and the owner's unsandboxed git reads it:
  the owner's terminal, editor, git apps, and the coding agents' own git run outside their sandboxes. The fence
  (`enforce.git: platform`, slices a–c, steps 3–6) is what makes those safe. Platform push doesn't need it: it never
  runs the owner's git in the project's repo.
- **Commits, config and brunna's own flows through the platform** (step 5: memory sync, `merge-branch`, `with-lock.sh`).
- **Network reads for sessions:** slice d's fetch, pull, clone and other-URL fetch.
- **A session socket.** With it, "sessions only" can become a real rule (open question 3), and a push request could move
  from the outbox to the socket for a synchronous answer. Neither is needed first.

**The broker's order of work, revised** (in `git-broker.md`, as an appended section; nothing above it is deleted):
1. Platform push (this doc). It needs nothing from slices b–d and goes first.
2. Slices b and c, then slice d without its push half (fetch, pull, clone, other URLs).
3. Steps 3–8 as before. Step 5's `merge-branch` push is the request above.

## 8. Residuals

- **Content the checks can't judge.** A push is a way out of the sandbox for data: a colleague's document committed to
  the repo leaves with it, and to a public remote that means publication. No pattern tells a document from code.
  - What bounds it: `branches`, the remote's own visibility, and open question 2's default for public remotes.
  - For a project that holds other people's data, keep the remote private or `branches: others`.
- **CI that already exists runs the pushed code** with the repo's secrets (`git-broker.md`, owner-run push, "What the
  gate doesn't stop"). Check 5 keeps the workflow files fixed, not what they execute.
- **The secret scan's limits** (check 7).
- **The boundary is the project** (section 3): a service or job of a project can request a push as its sessions can.
- **No rewrite of any branch.** An agent that rebased a branch it had already pushed pushes it under a new name.
  Rewriting only branches whose remote tip is the platform's own last push for this project would be safe (it's in
  `pushes.jsonl`), and is left for later.
- **Latency:** up to a minute for dispatch, plus the push.
- **The owner's credential** while open question 1 stands:
  - its reach is the whole account, bounded only by the checks and `paths.origin`;
  - a Keychain-stored credential fails while the screen is locked and becomes an owner to-do.
- **Not covered:**
  - the first push to an empty remote (`upgrade-idea` and onboarding make it);
  - tags and releases;
  - forge actions such as pull requests and issues (`gh` stays out of the sandbox);
  - remotes other than `paths.origin`.
- **Linux:**
  - the bundling job runs under Landlock (the owner form's end-to-end test already runs in `bin/linux-test`);
  - dispatch is a systemd user service, which usually has no ssh agent. The probe says so, and a file-stored token or
    an unencrypted deploy key is the owner's choice;
  - the fence (`enforce.git`) is still refused on Linux, which doesn't affect this.

## 9. Build order and tests

**Build order:**
1. `registry.validate_enforce` for `git_push` and the `brunna.json` limits (schema).
2. `git_remote.py`:
   - split `push` into `prepare` (registry, remote, bundle, plan) and `send`;
   - `push_credential`;
   - `bin/push_checks.py` (checks 5–9);
   - `serve <project>`;
   - `setup`.
3. `hubbus`: the `push` op in `take`, the queue, the worker's start, the result file. The client in the `brunna push`
   dispatcher, with the two forms.
4. `SESSION_SUBCOMMANDS` and the rules text, with the rendering report checked.
5. Doctor and status-page rows: per sandboxed project, `git_push` and the last push. The daily probe in
   `brunna/secrets`.
6. Onboarding: `connect`, `onboard-project.sh` flags, the skill's decision 5, `upgrade-idea --push`, and
   `connect_project`'s `none`.
7. `merge-branch`'s request on `push_on_merge` in a sandboxed project.

**Tests** (`tests/test_platform_push.py`, on top of `tests/test_git_remote.py`'s remote, http-backend and recording
credential helper). Every refusal comes with a plain-git control that pushes the same thing:
- **The outbox:**
  - a request lands for its own project only, and a project named in the file is ignored;
  - a malformed request is refused through `outbox_refused`;
  - a second request while one is queued is refused;
  - the daily cap holds;
  - the worker starts detached and outlives the dispatch run.
- **Each check of section 4**, with its fail-closed case:
  - gitleaks that exits 3;
  - the names list named but missing;
  - the anonymous probe failing (treated as public);
  - an empty emails list;
  - a secret added in one commit and removed in the next is still caught.
- **The sha pin:** the branch moved after the request, refused.
- **The field:** `none`, `others` against the default branch, `all`. Validation refuses each bad shape, and a
  `manifest.update` that sets `git_push` is refused.
- **The reply:**
  - the result file is written through no link (a planted symlink at `push/<id>.json` is replaced, not followed);
  - the letter is `info`;
  - an owner-fixable refusal raises one to-do per episode and closes it on the next pass.
- **The forms:** `setup` is never a project, `--owner` works from a project folder, and the owner form run inside a
  sandboxed session (real `sandboxed-run`) refuses before contacting the remote.
- **Isolation:**
  - the worker's git runs only in the owned repo with `--git-dir` (recorded at `run_git`, as built);
  - a declared secret path that the project replaced with a link is not followed;
  - a canary in the project's `.git` never runs;
  - the scanner reads no file from the workspace.
- **End to end:** a session's `brunna push` in a throwaway instance, through the real `sandboxed-run` (Seatbelt on
  macOS, Landlock in `bin/linux-test`) and the real dispatch.

A blind review of this design before building, and of the code before `merge-branch` (the working rule).

## Open questions for the owner

1. **Which credential the platform pushes with.**
   - **The owner's**, as the base above: no setup, and the same as `mirror-backup.sh` and `upgrade-idea.py` use today.
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
   - **What and where:**
     - one ed25519 key per project, made by the platform;
     - the private half at `<secrets>/credentials/<project>/git-push` (600), beside `credentials.py`'s grants. Every
       sandbox denies the secrets directory (`policy.secret_dirs`);
     - a record `git-push.json` beside it, holding the push URL, the host and the pinned host key.
   - **Declared by derivation, not by a `secrets:` entry:** a project whose `enforce.git_push.branches` isn't `none` and
     whose origin isn't a local path has the grant `git-push`. It is owner-only because `enforce` is, and
     `check-secrets.py` and `brunna credentials status` derive it as they derive the alert and provider keys. It is
     never in the cloud backup: re-granting is cheap, so a restore raises the to-do.
   - **How it pushes:**
     - `GIT_SSH_COMMAND` is `ssh -F /dev/null -i <key> -o IdentitiesOnly=yes -o IdentityAgent=none
       -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<record's pinned key>`;
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
     - a forge without `gh` support, or with no deploy keys: setup prints the public key and its fingerprint, and
       the owner adds it once. That is a person, because no rule can do it.
   - **The probe and the to-do:**
     - the daily probe is `ls-remote` through the grant;
     - "Permission denied (publickey)" or a changed host key breaks it: an owner to-do with `brunna push setup
       <project>`, one per episode (`raise_todo`);
     - a revoke, `brunna push setup <project> --revoke`, deletes the key at the forge and here.
   - **Build order:** added to step 2 (`push_credential`, `setup`) and step 6 (onboarding, `upgrade-idea`). Its tests
     use an sshd on 127.0.0.1 with the test key, plus a control in which the owner's agent key is the only one that
     would work, which must fail.
2. **The non-interactive default for a sandboxed project with a public remote.** **Recommended:** `others`.
   Publishing the default branch of a public repo is the one push no check can judge. Interactive onboarding still
   offers `all`.
3. **Who may request.** **Recommended:** the project as a whole, which is what the outbox can enforce. When the broker's
   session socket exists, an optional `git_push.from: sessions` can restrict it to sessions, for projects whose
   unattended jobs read outside content.
