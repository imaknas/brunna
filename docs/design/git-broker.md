# Git through the platform: a sandboxed project never writes a git dir

Status: design, 11-14, on measurements made the same day; r2 after a blind review the same day (at the end; it holds
where the text above disagrees: chiefly, no process writes both the worktree and the git dir, and
`safe.bareRepository=explicit` is a precondition again); r3 after a second blind review (at the very end; it holds
over both: an unsandboxed checkout can be raced into writing anywhere the owner can write, and how far to go against
that is the owner's choice; B-1's mitigation 3 built 11-14, mitigation 2 built and removed 11-15, the last sections);
approved by the owner 11-15; step 1 (the fence, behind the per-project switch) built 11-15, "Step 1 as built"; step
2's build list after it (four slices, open points; not built), and slice d's push half as an owner-run command,
built 11-15 ("Owner-run push, built first", at the end); slice d's session push superseded by
`platform-push.md` (design, 11-15; "Platform push supersedes slice d's session push", at the very end). It takes over the prevention half of
`git-config-fence.md` (the per-name fence, the narrow `brunna git` runner and the one-time install). That doc's scan stays
as a second layer, and so do its config allowlist and its presets. Measured in scratch repos (git 2.50.1, macOS on a
case-insensitive APFS volume). A sandboxed writer was stood in for by the platform's real Seatbelt profile
(`seatbelt.service_profile`), with the candidate rules appended. Codex 0.167's own sandbox was run through
`codex sandbox` (no model). Claude Code was read from its installed binary and not run. There were no model calls.

## The problem and the owner's decision

A sandboxed project's sessions, services and jobs may write their project's `.git` today. Whatever is written there,
the owner's unsandboxed git later reads and runs in that folder: their terminal, their editor, git apps, and the coding
agents' own git. Codex and Claude Code both run git as the user, outside their sandboxes (`runtime-codex.md` B1;
"Claude Code" below). `git-config-fence.md` tried to fence the dangerous names one by one and to detect the rest.
Two blind reviews (its r2 and r3) showed the same thing: detection that re-implements git's parsing always misses
something. So the guarantee has to come from preventing the writes. The index can't simply be fenced, because the
sandbox must commit.

**The owner's principle (11-14):** a user never has to remember a special way to use their agent. Any tool opened
normally in a sandboxed project must be safe and get full brunna: the Codex app or CLI, Claude Code, their editor, a
terminal, a git GUI. **Caveat (r3):** this design doesn't yet deliver that for checkouts. While a project process runs,
an unsandboxed checkout (the owner's, or an agent tool's worktree mode) can be raced into writing anywhere the owner can
write. That was true before this design too. The owner's options are in r3, "B-1: the options".

**The owner's decision (11-14):** git goes through the platform. In a sandboxed project, the sandbox can't write any git
dir at all. The `git` an agent runs inside the sandbox is a platform wrapper on its PATH. The wrapper forwards the
command to a platform service outside the sandbox, which runs the real git with safe settings and an allowlist. So a git
dir is only ever written by real git under the platform's control, or by the owner. Agents and the owner keep typing
ordinary git commands.

## What was measured (11-14)

### 1. How agents call git

**Claude Code** (read from the binary; needs a run to confirm):
- Its own git calls look the program up on PATH. There is a check that refuses a `git` found inside the working folder,
  so a wrapper in the platform's own folder would be accepted.
- They run with `-c core.hooksPath=/dev/null -c core.fsmonitor=` and `--no-optional-locks` (status, worktree add).
- Some calls also set `GIT_NO_REPLACE_OBJECTS`, `GIT_TERMINAL_PROMPT=0`, `GIT_SSH_COMMAND=false` and
  `GIT_LFS_SKIP_SMUDGE=1`, plus `safe.bareRepository=explicit` and `protocol.file.allow=never`, and they strip
  `GIT_DIR` and its kin.
- **Nothing in them turns filters off.** A clean filter in the repo's config would run from Claude's own status call,
  as the user, which is B1 again (Codex's case). Not run.
- Claude's worktree mode creates worktrees with its own git, as the user, outside the sandbox.
- The Bash tool's shell gets `GIT_EDITOR=true`.
- The sandbox's `allowUnixSockets` takes paths on macOS only ("ignored on Linux", says its own schema). Unix sockets are
  denied unless listed.
- The binary carries a list of git-dir names (`config.lock`, `config.worktree`, `commondir`, `remotes`, `branches`,
  `worktrees`, `alternates`) and a flag-gated "allow git config" switch for its sandbox. So Claude may already protect
  some of these itself. The platform doesn't rely on it.

**Codex** (`codex sandbox`, workspace-write, 0.167):
- The top repo's `.git` is read-only throughout, `.git/brunna/` included.
- `sub/.git` (a folder), a `.git` gitfile elsewhere and a file named `HEAD` were all created.
- A unix socket in the workspace was refused with the network off and **reachable with it on**, with no path rule.
- Codex's own git (the daemon's `status`, `rev-parse`, `remote`, `/diff`) runs as the user outside the sandbox
  (`runtime-codex.md` B1). It never goes through a wrapper on the tool shell's PATH, and the platform doesn't own the
  daemon's PATH.

**Tools that bundle a git library** (libgit2/pygit2, dulwich, isomorphic-git, go-git, JGit) write `.git` themselves.
Under the fence below they fail with "Operation not permitted", and their reads still work. **None was tried.** The
tools that shell out to `git` (pip and npm git dependencies, `gh`, pre-commit, uv) reach the wrapper. That too is
unmeasured, beyond the fence doc's uv and pip results.

### 2. Denying every git dir to a sandboxed writer (Seatbelt)

These rules were appended to the service profile:
- no write to any entry named `.git` or below it, matched anywhere: a regex on `/\.git(/|$)`;
- no entry named `HEAD`, anywhere;
- the platform's folder `<repo>/.git/brunna/` allowed again, with its own entry pinned and no `.git` below it.

**Refused** (`Operation not permitted`):
- writing `.git/config`, `git config`, a hook, `.git/info/attributes`;
- `git init sub`, `mkdir sub/.git`, `mkdir sub/.GIT`, a gitfile `sub/.git`;
- `git init` in the temp area, `git init --bare x.git` there (by the `HEAD` rule), a git-dir shape `shape/HEAD`;
- `mv .git g`;
- a hard link to `.git/config`, in the workspace and inside `.git/brunna`;
- a symlink in `.git/brunna` aimed at `../config`, written through;
- renaming `.git/brunna` (pinned);
- a linked worktree rewriting its own `.git` file;
- `git commit`, `git stash`, `git worktree add` with real git;
- reading another project's file.

**Worked:**
- real git's `status`, `log` and `diff` (exit 0; status just skips its opportunistic index refresh);
- status in a linked worktree;
- writing the outbox under `.git/brunna`;
- a test-runner style export with the index and the object folder both in temp (`GIT_INDEX_FILE` and
  `GIT_OBJECT_DIRECTORY` in temp, the repo's objects as an alternate: `read-tree`, `add -A`, `write-tree`,
  `checkout-index`). It writes nothing in a git dir.

**Found:** uv's cache marker (`.git/brunna/cache/uv/sdists-v<N>/.git`) is refused by the `.git` rule. The fence doc's
pinned exception for it carries over unchanged.

### 3. The prototype broker (scratch, not committed)

The pieces:
- **The wrapper**, `git`: a small Python program put first on the sandboxed shell's PATH. It sends argv, cwd, selected
  environment and stdin over a unix socket and streams stdout, stderr and the exit code back.
- **The service**, outside the sandbox. Local operations run under a **writer profile**: the project's own profile,
  plus write access to this repo's git dir, minus the names git runs or reads elsewhere (`config*`, `commondir`,
  `gitdir`, `hooks`, `info`, `modules`, `remotes`, `branches`, the alternates files, worktree admin pointers). The
  entries `objects`, `objects/info`, `refs`, `logs` and `worktrees` are pinned. Network operations to the registry's
  remote run as the owner.
- The hardened invocation: `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.pager=cat
  -c protocol.ext.allow=never -c credential.helper= -c safe.bareRepository=explicit -c submodule.recurse=false`,
  `GIT_CONFIG_NOSYSTEM=1`, a platform-written global config, `GIT_TERMINAL_PROMPT=0` and `GIT_EDITOR=:`.

Through the wrapper, from inside the fenced sandbox:
- **Worked:**
  - `status`, `diff`, `add`, `commit -m`, `commit -F -` (stdin);
  - `log`, `show`, `branch`, `switch -c`, `checkout`;
  - a 3-way `merge`, `rebase`, `stash`/`pop`, `reset --hard`, `restore`, `cherry-pick`, `tag`, `blame`;
  - `git -C sub`;
  - `worktree add` inside the project, a commit in that worktree, and `worktree remove`. Remove needed `unlink` on the
    admin pointers; a first version that denied them failed;
  - `fetch` and `ls-remote` of the registry's remote (as the owner); `push origin main`;
  - `pull` (the fetch as the owner, the merge under the writer profile);
  - `init` in the temp area followed by a commit (the repo made by the platform with an empty template);
  - `hash-object -w --stdin` with 300 KB of binary data; `git apply` of a binary patch from stdin;
  - `gc` (one warning: `info/refs` sits under the denied `info`, so only `info/attributes` should be denied there);
  - a 20,000-commit `git log`, streamed in 0.12 s (direct: 0.08 s).
- **With the editor set to `:`** (Claude's `true` does the same):
  - a non-fast-forward merge without `--no-edit`, a conflicted `rebase --continue` and a conflicted
    `cherry-pick --continue` all worked;
  - `commit` without `-m` and `tag -a` without `-m` stopped on an empty message, as they do under `GIT_EDITOR=true`.
- **Broken or incomplete in the prototype:**
  - `push -u`: the push lands, the upstream isn't recorded (config is the config op's);
  - `clone`: the fetch worked; the checkout tried to write tracking config and failed. It needs the
    `branch.autoSetupMerge=false` preset;
  - a clone of another project's repo was refused by the project's own read rules, as it should be.
- **Not tried:** `add -p` / `-i` and anything else needing a TTY; credentials against a real remote (the prototype
  cleared the credential helper); `submodule` (refused); merge-branch and the memory sync through the wrapper.

**Latency** (30 calls each, small repo):

| path | `rev-parse HEAD` | `status --porcelain` |
|---|---|---|
| direct, unsandboxed | 9.4 ms | 10.2 ms |
| real git inside the fenced sandbox | 9.6 ms | 10.8 ms |
| through the wrapper and the broker | 36.8 ms | 38.1 ms |

Of the broker path's extra ~27 ms, the wrapper's Python start is 9 ms and applying the writer profile with
`sandbox-exec` is 8 ms.

### 4. Arguments and environment

- **The writer profile carries the guarantee; the argument filter is a second layer.** With the filter turned off and
  only an **exec guard** in the writer profile (`(deny process-exec)`, then allow only `/usr/bin/git`, the Command Line
  Tools' `git` and git's exec-path):
  - `rebase --exe '<cmd>'` (an abbreviation git accepts) and `rebase -x'<cmd>'` stopped the rebase without running the
    command;
  - `grep -O'<cmd>'` failed with "cannot exec";
  - every canary stayed absent.

  The same guard let commit, merge, `rebase --continue`, cherry-pick, stash, gc and worktree add/commit/remove work.
  For gc, the one warning came from the prototype denying all of `info/`.
- **The guard stops a program from running under the broker. It doesn't stop git from writing the request into its
  own state.** A `rebase --exe '<cmd>'` over two commits, run with the filter off:
  - stopped at its first `exec` line, without running it;
  - left `exec <cmd>` in `rebase-merge/done` and in `git-rebase-todo`.

  The owner's own unsandboxed `rebase --continue` then **ran the canary**. So keeping `exec` lines out of state the owner
  might continue rests on the allowlist (exact spellings, abbreviations off) and on the broker's check after a stop
  (below), not on the guard.
- **Git's own abbreviation knob works:**
  - `GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS=true` turned `rebase --exe` into "disallowed abbreviated or ambiguous
    option";
  - `log --outp=…` is "unrecognized" in any case (diff-style options don't abbreviate).
- **Git's own path checks hold for the ways tried:**
  - `update-index --cacheinfo` refused `.git/x`, `.GIT/x`, `sub/.git/config` and `git~1/config`;
  - `update-index --index-info` ignored `.git/x` and `sub/.GIT/config` ("Ignoring path");
  - `read-tree` of a hand-made tree holding `.git/config` or `.GIT/config` failed with "invalid path".

  `mktree` does accept a `.git` entry: the object can exist, but it doesn't reach the index.
- `update-ref CONFIG HEAD` doesn't reach `.git/config` on this case-insensitive volume. Unsandboxed, git refused it
  because the existing file isn't a ref. Through the broker, the writer profile refused `CONFIG.lock` first: its ASCII
  rule matched the folded name, as the fence doc found.
- **The environment:** a caller's `GIT_DIR`, `GIT_TRACE=<.git/config>` and `GIT_EXTERNAL_DIFF` were dropped by the
  prototype, and nothing ran. Dropping them silently changes what a command does, though, so the design refuses or
  runs in place instead (below).

### 5. Identity

- **The socket is not the identity.**
  - With no unix-socket rule, project A's sandbox connected to project B's socket. The service profile allows outbound
    unix sockets, and Seatbelt's read denial of B's folder doesn't cover `connect()`.
  - A forged header naming B's folder then got B's file back.
  - A socket in a read-denied platform folder was reachable too.
- **A per-sandbox socket rule works where the sandbox can say it:** `(deny network-outbound (remote unix-socket))` plus
  an allow for the project's own socket path refused B's socket with EPERM. Claude's `allowUnixSockets` is the same
  kind of rule. Codex can't say it: with its network on, any socket is reachable.
- **A nonce in the project's own folder works in every sandbox.**
  - The broker sent a random nonce. The caller had to create a file of that name in the project's request folder
    (`<workspace>/.hub-run/git/`), which the broker read through no-follow directory descriptors (cap-runner's
    rule).
  - A's sandbox couldn't prove B, either by writing the nonce in its own folder or by aiming at B's folder.
  - B's sandbox could.
- **The kernel's view of the peer** (`LOCAL_PEERPID`, then `sandbox_check` on that pid; a private API, called from a
  small C helper since the call is variadic):
  - it told A's sandbox apart from an unsandboxed process, and answered correctly for reading or writing existing
    files (A may write its own file, may not read B's);
  - it answered "denied" to `file-write-create` even for an unsandboxed process;
  - it is pid-based.

  Not relied on.

### 6. The owner's tools afterwards

Each run planted the same set:
- a clean filter in `.git/config` with `.gitattributes` selecting it;
- `core.fsmonitor`;
- a `post-index-change` hook;
- `info/attributes`;
- a nested repo with its own fsmonitor.

The owner's git then ran `status`, `add`, `commit` and `diff` in the repo, and `status` in the nested repo:

| who wrote the plants | canaries that ran |
|---|---|
| an unfenced sandbox (the positive control) | filter, fsmonitor, hook, nested |
| a fenced sandbox, with real git and the shell | none |
| a fenced sandbox, through the wrapper (`git config …`, `git -c core.fsmonitor=… status` included) | none |

## The design

### The pieces

1. **The fence:** nothing in a sandboxed project's sandbox writes a git dir, at any depth, anywhere it may write.
2. **The wrapper:** the `git` that sandboxed processes find on PATH. Outside a sandbox it execs the real git unchanged.
3. **The channel:** a unix socket per project, plus a nonce proof of identity.
4. **The broker:** a platform service (`local.brunna.git-broker`, in brunna's registry services like cap-runner). It
   checks a request, then runs real git:
   - local operations under the project's **writer profile**, as the project;
   - network operations to the registry's remotes as the owner, with fixed arguments;
   - config changes through one allowlisted op.
5. **The policy port:** `policy.py` gains neutral entries. Each sandbox adapter renders them.

### The fence (what every sandbox of a switched-on project denies)

In neutral terms, `policy.git_fence`:
- **any entry named `.git`**, as the entry and everything below it, wherever the project may write: its workspace, its
  runtime folder, `allow_write`, the temp area. Seatbelt checks a rename only at its two ends, so a tree built elsewhere
  and moved in must hit the rule wherever it was built (the fence doc's measurement);
- **any entry named `HEAD`**, so no git-dir shape can be made under another name (the fence doc's S1);
- **one writable folder per repo**, `<common dir>/brunna/`:
  - its own entry is pinned (it can't be renamed or removed);
  - no `.git` below it, except uv's pinned marker (`git_fence_pinned`, as the fence doc measured);
  - it holds the outbox, the test runner's store and the tool caches, as today.

Each adapter renders it:
- **Seatbelt** (services, command jobs, cap-run): the measured regexes. A guarantee `git-dirs-fenced` in
  `guarantees()`.
- **Claude sessions** (`runtime_claude.settings`):
  - Edit denies and `denyWrite` on `**/.git` and `**/.git/**` under every write root, and on `HEAD`;
  - the `.git/brunna/` exception;
  - the socket in `allowUnixSockets`.

  To measure before relying on it: whether a glob denies the entry and everything below it, at any depth; whether the
  exception wins; and folded spellings. A canary in `conformance_claude.py`.
- **Codex sessions** (the rendered profile, `runtime-codex.md` Step 4): `deny` globs on `**/.git`, `**/.git/**` and
  `**/HEAD`, which r2 measured to work for nested names. Codex already makes the top `.git` read-only (measured), but
  not nested ones, gitfiles or `HEAD`. Its `.git/brunna/` exception needs a measurement: today Codex refuses the
  outbox there too. A canary in `conformance_codex.py`.
- **Landlock (Linux):** it can't deny a name inside a writable tree. Unsupported (see "Linux").

### The wrapper

- A platform program in a folder that holds only `git`, which no sandbox can write (beside the CLI wrapper, under
  `policy.cli_dirs`). It is put first on PATH for sandboxed processes: `sandboxed-run` for services and jobs, the
  session environment for each runtime (below).
- **`brunna git <args>`** is the same program under the CLI, for any agent or tool whose PATH the platform can't set.
- **Outside a sandbox** (`isolation.confined_now()` false, as the owner's tools and Claude's and Codex's own git are), it
  execs the real git with nothing changed.
- **Inside a sandbox**, it chooses one of three paths:
  - **In place** (real git, inside the sandbox, no broker):
    - a fixed set of read-only forms: `status`, `diff`, `log`, `show`, `blame`, `grep`, `rev-parse`, `ls-files`,
      `ls-tree`, `cat-file`, `for-each-ref`, `show-ref`, `describe`, `merge-base`, `rev-list`, `name-rev`,
      `config --get*` / `--list`, `branch` / `tag` / `stash` / `worktree` / `remote` / `reflog` in their listing forms;
    - every invocation whose environment selects a repo, index or object folder (`GIT_DIR`, `GIT_WORK_TREE`,
      `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`). Such a call runs
      unchanged, and if it tries to write a git dir it fails loudly with the sandbox's refusal. It is never forwarded
      with those variables dropped.

    Measured: real git's reads under the fence cost 10 ms against the broker's 37 ms, and they run as the project, so
    there is nothing to argue about.
  - **Forwarded:** everything else, to the broker.
  - **Refused loudly** (exit 2, "can't be done here (why); nothing was run"): a forwarded call carrying a variable that
    names a program or a file git would write (`GIT_EXTERNAL_DIFF`, `GIT_SSH*`, `GIT_ASKPASS`, `GIT_EXEC_PATH`,
    `GIT_TEMPLATE_DIR`, `GIT_CONFIG*`, `GIT_TRACE*` naming a file, `GIT_PROXY_COMMAND`).
- **When the broker is down**, writes say so and fail; reads still run in place.
- It is small and starts fast. The prototype's Python start was a third of the overhead, so a compiled helper or a
  shell front for the in-place forms is an implementation choice.

### Identity

The project a request is for is the project whose own folder the caller can write. Never a path or name the caller
sends. In order:

1. **The socket the caller can reach.** Each project's socket lives in a platform-owned folder
   (`<state>/git-broker/<project>/`). Every sandbox that can name socket paths may connect only to its own project's
   socket: Seatbelt's rule (measured); Claude's `allowUnixSockets` (unmeasured, but it is the same kind of rule).
2. **The nonce, which holds in every sandbox.**
   - On connect, the broker sends a random nonce. The caller creates `<workspace>/.hub-run/git/<nonce>`.
   - The broker opens the workspace, `.hub-run` and `git` through no-follow directory descriptors and checks for a
     regular file there. Only then does it read the request. The nonce file is removed.
   - This is cap-runner's rule ("the folder a request is found in is the project"), made per connection. It is what
     holds for Codex, whose sandbox reaches any socket when its network is on (measured).
3. **The kernel's view of the peer** (`sandbox_check`) is not used. It is a private API, pid-based, and some of its
   answers were unreliable (above).

An unsandboxed process can prove any project. It already has the owner's authority and could write that `.git` directly,
so this gives it nothing new. The broker then checks that the request's working folder, and any `-C`, resolve inside
that project's folders. It also checks that the repo's common dir is one the project owns: its workspace repo, its
worktrees, or a repo the broker made for it (recorded in `<state>/git-broker/<project>/repos.json`). Since no sandbox
writes a `.git`, the gitfiles and `commondir` pointers it reads to find that dir were written by git or the platform.

### What the broker runs, and as whom

- **Local operations** run under the **writer profile**: the project's own sandbox profile, applied by the broker from
  outside with `sandbox-exec` (cap-runner's way: a profile can't be nested inside an agent's sandbox), plus:
  - write to this request's common dir and, for a linked worktree, its admin dir;
  - minus the names git runs or reads elsewhere: `config*`, `commondir`, `gitdir`, `hooks/`, `info/attributes` (with
    `info` pinned), `modules/`, `remotes/`, `branches/`, `objects/info/alternates` and `http-alternates`, each
    `worktrees/<n>/{commondir,gitdir,config.worktree}`;
  - with `objects`, `objects/info`, `refs`, `logs` and `worktrees` pinned as entries;
  - **the exec guard:** no program runs but git's own binaries, and the platform's hook forwarder (below).

  So nothing project-chosen runs with write access to a git dir, whatever reaches git: a missed option, an alias or a
  todo line. **What git writes into its own state is a different matter**, and it isn't all inert: a rebase todo with
  `exec` lines runs on the owner's `--continue` (measured). Three things keep such lines out:
  - the allowlist (no `--exec` / `-x`, abbreviations off);
  - the editor callback's check of a returned todo;
  - **the check after a stop.** Whenever a broker-run rebase, cherry-pick or revert stops, the broker reads
    `rebase-merge/git-rebase-todo`, `rebase-merge/done` and `sequencer/todo`. These are git's own line format, written
    by git, not by the project. If any line is an `exec` or `x` command, the broker aborts the operation (composed:
    P1 puts the refs and the index back and removes the state folder, P2 restores the worktree; r3 S-f) and says
    why.

  Whether the sequencer of `cherry-pick` and `revert` accepts `exec` lines at all wasn't measured. Worktree content
  (`.gitattributes`, `.gitmodules`, symlinks) is read by git as the project, inside the project's own sandbox.
- **The broker's global config** (`GIT_CONFIG_GLOBAL`, a platform-written file; `GIT_CONFIG_NOSYSTEM=1`). It holds:
  - the owner's `user.name` and `user.email`, unless the project's own config has them;
  - `init.defaultBranch`;
  - `safe.bareRepository=explicit`, `protocol.ext.allow=never`, `protocol.file.allow=user`;
  - `core.hooksPath` (the forwarder's folder);
  - `core.fsmonitor=false`, `core.pager=cat`, `submodule.recurse=false`.

  It holds **no drivers** (`filter.*`, `diff.*.textconv|command`, `merge.*.driver`), **no aliases**, no
  `credential.helper` and no `include*`. A `.gitattributes` in the worktree can therefore select only drivers that
  don't exist, which git ignores (the fence doc measured this). An alias is a caller's shell habit: the wrapper expands
  none, and `git <alias>` is refused as an unknown subcommand.
- **Drivers defined in the owner's own global config** (git-lfs's filter, say) are the one place the two gits differ.
  The owner's git applies such a driver to the files the attributes select, and the broker's git doesn't. So a file the
  owner's git would store as an LFS pointer would be staged whole by an agent, and the two would show each other's
  copies as modified. **Unmeasured** (no git-lfs here). The choices are question 10: copy named drivers from the
  owner's global config into the broker's, where they run under the writer profile with the exec guard allowing that
  one program; or leave them out and say so in doctor.
- **Network operations** (`fetch`, `push`, `ls-remote`, and the fetch half of `pull`) to a remote the registry names run
  **as the owner, unsandboxed**:
  - fixed arguments: `GIT_DIR` set explicitly, the URL and refspec pinned with `-c` from the registry, no hooks,
    `--no-recurse-submodules`, `push --no-verify`;
  - the owner's credential helper and global config apply;
  - they touch only the git dir's objects and refs, never the worktree.

  `pull` = this fetch, then `merge` / `rebase` of `FETCH_HEAD` under the writer profile.
- **Fetch or clone from any other URL** runs under the writer profile: the project's own network, no credentials (the
  Keychain is blocked), its own read rules. `file://` and paths reach only what the project can read (measured: another
  project's repo refused). `ext::` and `--upload-pack` are refused, and the exec guard would stop them anyway.
- **Config changes** go only through the config op: the fence doc's allowlist table and value rules, installed through
  `config.lock` created exclusively, never in place. The broker's own writes use git's lock files, so the owner's
  concurrent git waits or fails as it would against another terminal.

### The allowlists

**Recommendation: an allowlist**, not a denylist. A denylist is the parser-re-implementation the fence doc's reviews
kept defeating. The exec guard and the denied names carry the guarantee. The allowlist keeps caller-chosen content out
of the files git reads back as instructions and leaves writable: the index (r2 B1), `rebase-merge/` and `sequencer/`
todos (exec lines run on the owner's `--continue`).

- **Subcommands.** Allowed: the in-place set above, plus `add`, `rm`, `mv`, `commit`, `restore`, `reset`, `switch`,
  `checkout`, `branch`, `tag`, `merge`, `rebase`, `cherry-pick`, `revert`, `stash`, `apply`, `am`, `notes`, `gc`,
  `fsck`, `count-objects`, `hash-object`, `update-index`, `read-tree`, `write-tree`, `symbolic-ref`, `update-ref`,
  `format-patch --stdout`, `range-diff`, `init`, `clone`, `worktree`, `fetch`, `pull`, `push`, `ls-remote`, `remote`,
  `config`, `version`, `help`. Everything else is refused, by name:
  - `filter-branch`, `difftool`, `mergetool`, `hook`, `bisect run`, `submodule` (question 5);
  - `daemon`, `instaweb`, `send-email`, `credential*`, `http-backend`, `upload-pack`, `receive-pack`, `shell`,
    `remote-*`;
  - `fast-import`, `archive --remote`, `bundle`, `replace`;
  - `maintenance start|register` (it writes the owner's global config and schedules jobs).
- **Options:** one table per subcommand of exact spellings and their arity.
  - `GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS=true` is always set, so git itself refuses abbreviations (measured).
  - Short clusters and sticky values are parsed against the table, never passed through.
  - `--` and `--end-of-options` end option parsing; pathspecs after them are free (git refuses paths outside the repo).
  - Absent on purpose:
    - every option that writes a caller-named file: `--output`, `-o` / `--output-directory`, `--index-output`, `--prefix`
      on `checkout-index`, `--export-marks`;
    - every option that runs a program: `--exec` / `-x`, `-O`, `--ext-diff`, `--textconv`, `--upload-pack`,
      `--receive-pack`, `--config`;
    - every option that selects another repo or work tree: `--git-dir`, `--work-tree`, `--separate-git-dir`,
      `--reference`, `--template`;
    - signing: `-S`, `--gpg-sign`, `-s` on tags (question 6).
- **Global options:** `-C <dir>` (resolved and checked), `--no-pager`, `-P`, `--no-optional-locks`,
  `--literal-pathspecs`, and `-c` for an inert-key allowlist only: `user.name`, `user.email`, `color.*`,
  `core.quotepath`, `advice.*`, `init.defaultBranch`, `rebase.updateRefs`, `merge.conflictStyle`, `diff.renames`,
  `commit.gpgSign=false`, `log.showSignature=false`. `merge-branch` uses `rebase.updateRefs`.
- **Composed operations**, where git would write config itself:
  - `push -u` and `branch -u` / `--set-upstream-to`: the ref part runs under the writer profile, the upstream through
    the config op;
  - `branch -m` / `-M`: the ref rename, then the config op moves the section;
  - `switch` / `checkout` of a remote branch, with the preset `branch.autoSetupMerge=false`; a requested `--track` goes
    through the config op;
  - `remote add` / `set-url`: only a URL the registry declares (the fence doc's B4);
  - `config <key> <value>`: the allowlist.
- **The environment** passed on: `GIT_AUTHOR_*`, `GIT_COMMITTER_*`, the pathspec variables, `GIT_MERGE_AUTOEDIT`, the
  locale, `TZ`, `NO_COLOR` and `TERM`. Everything else is either handled in place, or refused (the wrapper's list). It
  is never dropped silently.

### init, clone, worktrees and scratch repos

- **`init <dir>`:** the broker creates the repo itself, as the owner, with `git init --template=` (no hooks copied;
  `git init` runs nothing) and the presets, then records it under the project. `<dir>` must resolve inside the
  project's folders or its per-run temp folder (question 3). It must be new or empty.
- **`clone <url> <dir>`:**
  - the broker's `init`;
  - then `remote.origin.url` through the config op (a registry remote, or a URL that passes the value rule);
  - then the fetch: as the owner for a registry remote, otherwise under the writer profile;
  - then the checkout under the writer profile.
- **`worktree add <path> [-b <branch>]`:**
  - `<path>` must resolve inside the project's folders, outside every git dir, and not exist yet (the fence doc's S6:
    walked through directory descriptors);
  - the broker's writer for this op may create `worktrees/<n>/` with its `commondir` and `gitdir`, which git writes
    from the path the broker checked;
  - the checkout runs under the writer profile.
- **`worktree remove` and `prune`:** may unlink those files (measured: needed).
- **The tools' own worktree modes** (Claude's, the Codex app's) create worktrees with their own git, as the user, outside
  any sandbox. That reads a trustworthy git dir and writes what git writes. A worktree outside the project's folders
  (the Codex app's) is refused by the broker, as the test runner already refuses one.
- **Scratch repos** (test suites' `git init` in temp): the broker makes them, as for `init`, but only in the project's
  per-run temp folder. They are recorded, and dropped when that folder goes. The temp area is shared between projects,
  so a recorded repo of A's is never served to B.

### Hooks and the platform's gates

- `core.hooksPath` points to a platform-owned folder in which every hook name is one program, the **hook forwarder**.
  The exec guard allows it, and it does one thing: it sends the hook's name, arguments, stdin and `GIT_INDEX_FILE`
  back to the broker over the request's own channel, then waits for a verdict.
- The broker runs the actual check **outside the writer profile**:
  - the commit gate (`brunna test --index`, with that index) in the plain project sandbox;
  - the commit-msg title check in the broker itself;
  - `pre-push` for the hub only.

  So the gate keeps git's exact semantics (`commit -a` and pathspec commits included), and nothing the project's tiers
  run has write access to a git dir.
- **The project's own hooks** (pre-commit, husky): installers can't write `.git/hooks` any more. A project that wants
  them declares a tracked hooks folder in its registry entry. The broker runs those hooks through the same forwarder, in
  the plain project sandbox. The owner's own git outside doesn't run them, since the config is the platform's.
  Question 4.
- The platform's hook installer (`render-project-settings`, `project_git hook-write`) stops writing `.git/hooks` in a
  switched-on project. The folder is the broker's.

### Editors, pagers, TTY

- **The editor:** git runs with `GIT_EDITOR=:` and `GIT_SEQUENCE_EDITOR=:`.
  - Measured: a merge, `rebase --continue` and `cherry-pick --continue` go through.
  - `commit` and `tag -a` without a message stop on an empty message, exactly as with Claude's `GIT_EDITOR=true`.
  - A caller whose editor is anything else (`EDITOR`, `VISUAL`, `GIT_EDITOR`, `GIT_SEQUENCE_EDITOR` set to a program)
    gets it run **on the caller's side** through the same callback channel. The forwarder hands the file to the
    wrapper, which runs the editor inside the caller's own sandbox on a copy and sends the result back.
  - An interactive rebase's todo coming back is checked: no `exec` / `x` lines (`break`, `label`, `reset`, `merge`,
    `update-ref` are fine).
  - Until that is built, such a call is refused with the reason, never silently accepted unedited.
- **The pager:** off (`core.pager=cat`). Paging is the caller's terminal's business.
- **Colour:** `color.ui=always` only when the wrapper's stdout is a terminal.
- **`add -p` / `-i`, `commit -p`, `checkout -p`** need an interactive loop on a terminal. Refused at first, with the
  `git apply --cached` way said. A pty relay later, if wanted (question 7).

### Streaming, timeouts, concurrency

- Frames: stdout, stderr, exit status, and stdin as a stream (binary is fine; measured). Nothing is buffered whole.
- Every call has a timeout (10 minutes, longer for `gc`, `fetch` and `clone`; a registry setting). The process group is
  killed at the timeout, and on a FIFO include, as `project_git` does.
- At most N calls per project at once (8 by default), queued beyond that. Git's own lock files (`index.lock`, ref locks,
  `config.lock`) order the broker against the owner's concurrent git, as two terminals are ordered. No extra lock.
- Every call is logged in `<state>/git-broker/log.jsonl` (project, subcommand, the path taken, exit code, duration; no
  arguments' values). Refusals are counted for the status page.

## What each runtime needs

- **Services and command jobs:** the Seatbelt fence and socket rule in `service_profile`, and the wrapper first on PATH
  in `sandboxed-run`. Fully platform-controlled.
- **Claude Code sessions:** the fence in settings, the socket in `allowUnixSockets`, and the wrapper on the Bash tool's
  PATH. That last one must survive the shell snapshot re-reading the user's rc files: an `env` setting, or the
  session-start hook's environment file. Unmeasured. Claude's own git (the status at start, worktree mode) stays outside
  and reads a trustworthy git dir, so its missing filter protection stops mattering.
- **Codex sessions:**
  - the fence globs in the rendered profile;
  - the network on, which D8 already decided and which the socket needs (measured);
  - the nonce for identity;
  - the wrapper on the tool shell's PATH (`shell_environment_policy` in the rendered profile; unmeasured).

  **B1 changes meaning:** Codex's own git still runs outside the sandbox, as the user, through no wrapper, and that is
  fine once the git dir is written only by git under the platform's control. The filter canary turns from "a planted
  filter runs" into "the sandbox can't plant one".
- **Any other agent:**
  - if the platform can't set its PATH, `brunna git …` is the explicit form, and the rules text says so;
  - plain `git` there still reads in place, and its writes fail with "Operation not permitted";
  - that is a loud failure, not a hole.

## Platform flows that change

- **`project_git` ops that write** (`hook-write`, `hook-remove`, `set-origin`, `exclude`, `worktree-add`,
  `worktree-remove`, the commit in `sync-memory`) move from "a job as the project" to the broker's runner. The platform
  calls it in-process, with the same writer profile and config op. The read ops (`head`, `worktrees`, bundles, facts)
  stay jobs as the project. They only read, and the fence makes their opportunistic writes fail harmlessly.
- **Memory sync:** the `.agent-memory` worktree's admin dir (worktree form) or the memory clone's git dir (clone form) is
  a git dir like any other, fenced for the project. Its commit of `memory/` and `STATE.md` goes through the broker's
  runner. Its bundle is still made as the project.
- **The commit gate** (r2 B3 as built): it runs through the hook forwarder. The test runner's export stops writing objects
  into the repo: its index and its object folder go into the run folder, with the repo's objects as an alternate
  (measured to work under the fence).
- **`merge-branch`:**
  - its git goes through the wrapper (it calls `git` on PATH);
  - `-c rebase.updateRefs=false` is on the inert list;
  - its lock moves from `<common>/merge-branch.lock` to `<common>/brunna/`;
  - its dry run's temporary worktree moves out of the git dir, or the dry run tests the rebased commit with `--rev`
    and no worktree at all;
  - the fast-forward of main runs in the main checkout through the broker (the same project's repo).
- **`with-lock.sh`:** its lock folder moves from the git dir to `<git dir>/brunna/`.
- **`sync-local-setup`:** its `info/exclude` writes become the broker's `exclude` op.
- **The fence scan** (`git_fence.py`, built): it stays as a second layer, report only. In a switched-on project, any
  finding means a writer got past the fence, so it is an alert, not a note. B1's index check becomes a check that the
  index was written by git: nothing else can write it.

## What stays residual

- **Arbitrary writes as the owner during any unsandboxed checkout (r3, B-1).** This covers the owner's terminal, editor
  or git app, and an agent tool's own worktree mode. While any of the project's processes runs (a session's command, a
  service, a job), a checkout can be raced into writing the project's files into any folder the owner can write:
  `~`, `~/Library/LaunchAgents`, another project. Existing files there get overwritten. This holds today, with or
  without the broker. The options are in r3.
- **Worktree content read by the owner's own git.** The owner's global config is theirs. A tracked `.gitattributes` can
  select a driver it defines (git-lfs's filter, say), which then runs as the owner on content the project chose. The same
  goes for `.lfsconfig` (not measured: no git-lfs here) and the URLs in `.gitmodules` on the owner's own
  `submodule update`. This is the fence doc's question 3, unchanged.
- **Git's own parsers and path checks**, run as the owner on objects, packs and an index git wrote from project content.
  Git's robustness, as before. The exploit classes that needed a planted index or config are closed by prevention.
- **Tools that bundle a git library** inside the sandbox fail their writes loudly (unmeasured per tool). The rules text
  points at archive URLs and the wrapper.
- **The broker runs git as the owner** for network operations, in fixed forms, with the owner's credentials (question 1
  limits where).
- **A tool that runs the real git by an absolute path** inside the sandbox gets reads, and writes refused. A nuisance,
  not a hole.
- **The temp area's shared folders:** another project can't write a recorded scratch repo's git dir (the fence), but can
  write its worktree files, as anything in the shared temp area can be written today.
- **Linux:** no prevention (below).
- **What runs outside every sandbox in the folder** (a build or test the owner starts) is outside this design, as always.

## How this changes the fence plan and Codex's confined check

| `git-config-fence.md` part | under git through the platform |
|---|---|
| the per-name deny list and "what stays writable" | replaced by the whole-git-dir rule |
| B2's symlink rule and `logs` / `refs` / `objects` pins | unnecessary for the sandbox (it writes nothing there); the pins stay in the writer profile |
| presets (`push.default`, `branch.autoSetupMerge=false`, …) | kept, written by the config op |
| the config allowlist table and URL rule (B4) | kept: the config op's rules |
| the narrow `brunna git` runner and its "when it may write" condition | subsumed by the broker. A switched-on project is fenced by construction. |
| the one-time install | becomes the rebuild (below) |
| B1 (index entries) | prevented, as far as tried: only git writes the index, and the four ways tried (`update-index --cacheinfo` / `--index-info`, `read-tree` of a hand-made tree) refused or ignored the entries; any other way is git's own path check |
| the scan | kept as a second layer; a finding is an alert |
| S1 (`HEAD` shapes), S3 (scratch repos), S6 (worktree paths) | taken into the fence and the broker |
| the uv marker exception | kept |
| Linux | still unsupported |

**Codex's confined check** (`runtime-codex.md` Step 4) gains one condition: **the project's git goes through the
platform.** That means:
- the switch is on and the rebuild is done;
- every writer's fence is rendered, with each runtime's canary passed for the version running (Seatbelt; Claude's
  settings; Codex's profile);
- the broker is up;
- the machine is macOS.

The scan finding nothing is not a condition (the fence doc's r3); a scan finding is a failure. D13 is re-decided then
(`runtime-codex.md`).

**Caveat (r3, until the owner decides B-1):** these conditions don't cover the owner's checkout race. While any of the
project's processes runs, an owner's or an agent tool's unsandboxed checkout can be made to write the project's files
anywhere the owner can write. Which conditions "confined" adds for that is the owner's choice (r3, "B-1: the options").

## Rollout per project, and the way back

- **The switch:** `enforce.git: platform` in the project's registry entry. It is an owner-only security field like the
  rest of `enforce` (manifest.update can't change it). Off by default.
- **Switching on**, refused while a session is open in the project (`hubconf.cwd_users()`) or while a rebase, merge,
  cherry-pick or bisect is in progress:
  1. **The rebuild.** Every git-dir file made before the switch could be a hard link or a symlink to somewhere else (the
     fence doc's "State from before the fence"), and that now matters for every file, the index and `HEAD` included.
     So the broker builds a **fresh git dir** with git:
     - `init --template=`, then a fetch of every ref from the old one (packs, not hard links);
     - the config from the allowlisted keys only;
     - the index from the old one's `ls-files --stage` through `update-index --index-info` (git checks every entry);
     - each stash entry through `stash store`, and `info/exclude` as text;
     - the platform's own folder, `brunna/` (unsent outbox letters, the test runner's store, the tool caches), each
       file copied as a fresh file, links not followed;
     - each linked worktree's admin dir through `worktree add --no-checkout` at its existing path, then
       `worktree repair`.

     The old git dir is moved into the platform's state (`.state/git-broker/<project>/before-<time>/`), and the new one
     is put in its place. Reflogs aren't carried (they are history, not state). **Unmeasured.** Building and checking it
     is step 2 of the order of work.
  2. Then the fence is rendered for each writer, and the wrapper put on each writer's PATH.
  3. Then brunna's own ops move to the broker, and the rules text changes for that project.
- **The way back:**
  - switching off removes the fence and the wrapper from every rendering, and the broker stops serving the project;
  - the git dir stays as it is, a plain git dir;
  - `brunna git-broker restore <project> --at <time>` puts the pre-switch git dir back, for the case where the rebuild
    lost something.
- **Doctor and the status page:**
  - per sandboxed project: "git through the platform: on (rebuilt <time>) / off / not supported on this OS";
  - each writer's fence canary for the running version, and the count of refusals in the last day with the most
    common one;
  - the broker service's health, as cap-runner's.

**The rules text** for a switched-on project (in `templates/agent-instructions.md`, sandboxed projects only):

> Git works as usual here. The platform runs it for you, so a few things are different:
> - Commit with `-m` or `-F`.
> - A new branch has no upstream: push with `git push`, and pull with `git pull origin <branch>`.
> - Hooks and git settings are the platform's. `git config` accepts only a short list of keys.
> - Some commands and options aren't run (the refusal says which, and why).
> - Git dependencies need an archive URL in place of `git+…`.
> - If plain `git` fails with "Operation not permitted", run `brunna git …` instead.

## Linux

Landlock only grants rights. A write right on the workspace covers its `.git` at every depth, and no name inside a
writable tree can be denied (`landlock.py`'s header). A mount namespace could bind `.git` read-only, but unprivileged
user namespaces are restricted on current Ubuntu (`capabilities.md`). Also, Claude's own sandbox can't filter unix
sockets by path there. So on Linux the switch is refused with "not supported on this OS". The scan runs as it does today,
and a sandboxed project's sessions of a runtime whose git runs as the user count as not confined (the fence doc's
Linux section, unchanged).

## Tests

- **`policy.git_fence`** and each rendering: Seatbelt regexes, Claude rules under every write root, Codex globs,
  Landlock reporting `Unsupported`; the boundary ratchet.
- **Seatbelt integration** (macOS, as `tests/test_control_files.py`):
  - every refused probe in section 2, each with an unfenced positive control;
  - the in-place reads and the temp-object export working;
  - the uv marker exception.
- **The broker**, with a stand-in project in a throwaway instance:
  - every row of section 3, through the wrapper from inside the fence;
  - identity: another project's socket refused by the socket rule; a forged header with no proof refused; a nonce
    aimed at another project's folder refused; a FIFO or a link at `.hub-run` or `git` refused without blocking;
  - the exec guard: `--exe`, `-x<cmd>`, `-O<cmd>`, `!` aliases and an `exec` todo line, each run with the argument
    filter turned off. No canary runs, either under the broker or in the owner's `rebase --continue` afterwards: the
    check after a stop aborts a rebase whose state holds an `exec` line.
  - the allowlists: unknown subcommands and options, abbreviations, sticky shorts, `-c` keys and environment variables
    are each refused with the reason. Forms with a selecting environment run in place.
  - the config op's allowlist and install (from the fence doc);
  - the composed operations, the hook forwarder (the commit gate's verdict, a project hook), the editor callback with
    an `exec` line refused;
  - timeouts killing the group; a FIFO include; concurrency against an owner's git holding `index.lock`.
- **The owner's canaries** (section 6) after every scenario, and the same plants written with no fence (positive
  control).
- **The rebuild:** hard links and symlinks from before are cut; staged changes, stashes and worktrees survive; an
  in-progress operation refuses; restore.
- **Conformance canaries** per runtime and version, run from inside that runtime's sandbox in a throwaway project:
  - the section 2 probes and the socket rule;
  - the positive controls (`.git/brunna/` written, a commit through the wrapper);
  - the wrapper being the `git` found on PATH.
- **Golden output** for refusals, the rules text and the status rows.

## Order of work

1. The fence in `policy.py` and the Seatbelt rendering, with its tests. The fence isn't applied anywhere yet.
2. The broker and the wrapper: the channel and the nonce, the writer profile with the exec guard, the allowlists, the
   config op, the composed operations, the network path. Plus the rebuild and its restore. Tested against throwaway
   projects only, answering "not switched on" for every real one.
3. The hook forwarder, with the commit gate on it. The test runner's export moved to its own object folder.
4. Each runtime: Claude's settings and PATH, Codex's profile and PATH, each with its canary. A runtime whose canary fails
   keeps the project's sessions of that runtime refused (as hub-event does today).
5. brunna's own flows: `project_git`'s write ops, memory sync, `merge-branch`, `with-lock.sh` and `sync-local-setup`
   onto the broker or into `.git/brunna/`.
6. Switching on one throwaway project end to end. Then the owner's first real project.
7. Codex's confined check gains its condition (`runtime-codex.md` Step 4 r2's steps 3–5).
8. The editor callback, then (if wanted) a pty relay.

A blind review of this design before step 1, and of the code before `merge-branch` (the working rule).

## Open questions for the owner

1. **Pushing with your credentials.** The broker pushes to the registry's remotes as you, for any process of the
   project, services and jobs included, which today can't reach your credentials.
   - **Recommended:** sessions and jobs alike, but never a forced push and never to the default branch, unless the
     registry says so (`push_on_merge` already exists for `merge-branch`). `--force-with-lease` is allowed to the
     project's own non-default branches.
2. **Fetch and clone from other URLs.** **Recommended:** yes, in the project's own sandbox (its network, no credentials,
   its read rules), https only.
3. **Scratch repos.** **Recommended:** made by the broker only in the test runner's per-run temp folder and the
   project's own folders, never elsewhere in the shared temp area.
4. **The project's own hooks.** **Recommended:** run through the forwarder in the project's sandbox when the registry
   declares a tracked hooks folder. Never installed into `.git/hooks`.
5. **Submodules.** **Recommended:** none at first (the fence doc's question 6).
6. **Signing.** Signed commits and tags would use your key from a project's request. **Recommended:** not through the
   broker.
7. **Interactive git** (`add -p`, editors on a terminal). **Recommended:** the editor callback in step 8. A pty relay
   only if you use these inside a sandboxed session.
8. **Your global drivers** (git-lfs and the like) still run as you, in your own git, on project content (the fence doc's
   question 3). **Recommended:** listed and accepted.
9. **Switching on.** **Recommended:** you flip it per project. The rebuild runs then, with nothing open.
10. **Drivers from your global config inside the broker** (git-lfs and the like). **Recommended:** copy only drivers you
    name, per project in the registry, so that an agent's staging matches yours. A named driver runs under the writer
    profile, with the exec guard allowing that one program. Without one, doctor says the two gits differ for that
    project.

## Risks

- **Friction:** a refusal for an option an agent uses legitimately. The allowlist grows from the refusal log, one
  reasoned entry at a time, as the fence doc's config allowlist does.
- **One more service on the critical path.** If it is down, sandboxed projects can read but not write git. It is
  watched like cap-runner.
- **Latency:** forwarded calls cost ~37 ms against 10 ms (measured). Reads run in place.
- **Seatbelt** is deprecated as a public interface. The platform already depends on it for every sandbox.
- **Each runtime's sandbox semantics can change** between versions: the per-version canaries hold a version back.
- **The rebuild** touches every switched-on project's git dir once. It is reversible from the kept copy, and unmeasured.
- **The nonce file** is one more write the project makes per call into its own `.hub-run/git/`. It is removed by the
  broker, and a project that floods it only slows itself.

## r2 (11-14), after the blind review

A cold review found two blocking points (B1, B2), both reproduced end to end, eight should-fix items (S1–S8), nits
and a rework of the owner questions. Each fix was measured in scratch the same way as above: git 2.50.1, the platform's
real `service_profile` plus the candidate rules, the reviewer's probes re-run against the changed prototype. Where r2
and the text above disagree, r2 holds.

### B1: a symlink swap during a broker checkout. Fixed by splitting every operation into two processes.

**The finding.** The project loops, replacing a real folder `d` with a link `ln -s .git d`. Meanwhile the writer
profile runs `checkout -f` of a tree holding `d/…`, including `d/rebase-merge/` with an `exec` line in its todo.
- Git caches its leading-path check, so some of the tree's files land inside `.git/`.
- The writer profile allows them there: one process may write both the worktree and the git dir, and Seatbelt checks
  the resolved path.

Re-run here, the first round put 2,931 of the tree's files and a `rebase-merge/` into `.git`. The owner's `git status`
then reported a rebase in progress. The reviewer's run went on to the owner's `rebase --continue` running the canary;
here the follow-up probe stopped earlier, at "merge conflicts". Every operation that writes the worktree can plant
this way.

**The fix: no single process writes both the worktree and the git dir.**
- **P1, the git-dir writer.**
  - It may write the request's git dir (minus the names git runs or reads elsewhere, as before) and nothing else: no
    worktree, no temp area, only `/dev/null` and the terminal devices.
  - The exec guard applies.
  - A symlink in the worktree gives it nothing, since it writes no path through the worktree.
- **P2, the materialiser.** It is the project's own fenced profile: the profile its sessions and services already
  have. It writes the worktree from a **temporary copy of the index** outside every git dir:
  - `read-tree -m -u <old> <new>`, or `read-tree -u --reset <new>`, with `GIT_INDEX_FILE` set to the copy;
  - `checkout-index` for single paths.

  A swapped link reaches no git dir, since the fence refuses that write.
- **The order:**
  1. P1 computes the next index beside the real one (`.git/index.broker`, while the worktree is still the old one;
     `read-tree -m` checks the worktree is up to date, so it can't run after P2).
  2. P2 materialises.
  3. **P2 checks its own work:** `diff-files --quiet` against the temp index, which is S8, below.
  4. Only then does the broker rename the next index into place and move the refs, and P1 refreshes the index's stat
     information.
  5. If P2 failed or the check failed, the next index is dropped and no ref moves. The call fails.
- **Operations that don't write the worktree run whole under P1**, with the worktree read-only: `commit` (`-a` too),
  `add`, `rm --cached`, `branch`, `tag`, `notes`, `update-ref`, `stash create` / `store` / `drop`, `fetch`, and the
  reads.
- **Operations that write the worktree are composed from plumbing:**

  | operation | P1 (git dir only) | P2 (worktree only) |
  |---|---|---|
  | `switch` / `checkout <branch>` | next index (`read-tree -m`), then `symbolic-ref HEAD` | `read-tree -m -u` on the temp index |
  | `reset --hard <rev>` | next index (`read-tree --reset`), `update-ref HEAD` | `read-tree -u --reset` |
  | `restore <path>` | refresh | `checkout-index -f -- <path>` |
  | `merge` | `merge-tree --write-tree` → `commit-tree` → next index → `update-ref`; on conflict, the stage entries through `update-index --index-info`, with `MERGE_HEAD` / `MERGE_MSG` written by the broker | the merged tree (with conflict markers) |
  | `rebase` (no conflicts) | `replay --onto` → `update-ref --stdin` | the new head's tree |
  | `stash push` / `pop` | `stash create` / `store`; `merge-tree --merge-base`; `stash drop` | reset to HEAD; the merged tree, index kept |
  | `pull` | the fetch; then as `merge` | as `merge` |
  | `clone` | `init` by the broker; fetch | `read-tree -u --reset` of the default branch |
  | `rm` / `mv` (in the worktree) | `rm --cached` / `add` | the file removed or renamed as the project |
  | `apply` (worktree only) | none | `git apply` itself |

- **Measured, split this way.** All of these worked:
  - clone, checkout both ways, `commit -a`;
  - a clean merge, and a conflicted merge (`UU f`, markers in the file), then resolved and committed with two parents;
  - a rebase through `replay`;
  - `stash push` / `pop` (contents back);
  - `reset --hard`, `restore` (worktree clean afterwards);
  - `pull`.

  Each operation took 3–6 sandboxed git processes. The ~8 ms of applying a profile each time dominates.

  | | ms |
  |---|---|
  | clone | 155 |
  | checkout | 83 |
  | commit -a | 21 |
  | merge, clean / conflicted | 116 / 93 |
  | rebase | 122 |
  | stash push / pop | 107 / 92 |
  | reset --hard | 91 |
  | restore | 29 |
  | pull | 165 |
  | the same nine commands directly, unsandboxed, in one process each | 166 in total |
- **The race, re-run against the split (probe B/C's attack, 40 rounds, 90 s of swapping):**
  - no file of the tree in `.git`, and no `rebase-merge/`;
  - every round's checkout was refused as partial, and HEAD never moved;
  - the control without the attacker completed 40 of 40;
  - the reviewer's follow-up probe on the result: an owner's `status` clean, `checkout -f HEAD` clean, no rebase in
    progress, no canary.

  So the attack becomes a refusal: the project can only stop its own checkouts.
- **A fetch from a path** can't run under P1. Git starts `upload-pack` through `/bin/sh`, and allowing a shell under the
  exec guard would void it, since a shell's builtins write files. So the source is read under the project's own
  profile into a bundle (`bundle create`), and P1 fetches the bundle (measured, for the clone and the pull above).
- **Not measured yet:**
  - a rebase with conflicts (`replay` stops; a stepwise `merge-tree` per commit with the `rebase-merge/` state written
    by P1 is the plan, so the replay's being experimental in git is no lock-in);
  - cherry-pick and revert (the same `merge-tree` shape);
  - `stash pop` with conflicts;
  - `switch -c` with local changes;
  - `pull --rebase`;
  - `worktree add`, where the broker makes the folder and writes its gitfile through no-follow directory descriptors,
    P1 the admin dir, and P2 the checkout.

  Each is in step 2 of the order of work, with the race probe run against it.
- **Residual, newly listed: the owner's own unsandboxed checkouts race the same way.** r3 measured this as worse than
  written here: the swap can point at any folder, not only `.git`. The result is arbitrary writes as the owner. See r3,
  B-1.

### B2: git-dir shapes despite the "no HEAD" rule. `safe.bareRepository=explicit` is a precondition again.

**The finding** (re-run here):
1. **A HEAD path committed through the broker.** A commit whose tree holds `x/HEAD` (made with
   `update-index --cacheinfo`) passed through the writer profile. A checkout under the writer refused to write
   `x/HEAD`, with exit 0 (S8). An unsandboxed `worktree add` (the owner's, or an agent tool's worktree mode) wrote it.
   The fenced project then added `objects/`, `refs/` and a config with `core.worktree=..` and `fsmonitor` around it,
   and the owner's plain `git status` in `x/` **ran the fsmonitor**.
2. **A folder holding an existing HEAD renamed in.** The sandbox moved a folder already holding a `HEAD` (a fixture)
   into place and planted the config. The owner's `status` inside it ran the fsmonitor under git's default;
   **`safe.bareRepository=explicit` refused it** ("cannot use bare repository").

**Folded in:**
- **The precondition.** `safe.bareRepository=explicit` in the owner's global git config is a precondition of
  "confined" (the fence doc's question 2, answered). **The owner set it on this machine on 11-14.**
  - Doctor checks it on every run.
  - Its absence turns the project's row and Codex's confined check to "not confined", with the one command that fixes
    it.
  - The platform never writes it: it is a user-level setting.
  - A git GUI or editor that reads repos through a library that ignores this setting is a residual: listed, not
    covered.
- **The HEAD rule, non-directories only** (the fence doc's S1 wording restored: a folder named `HEAD` is ordinary).
  Measured with `(deny file-write* (require-all (regex #"/HEAD$") (require-not (vnode-type DIRECTORY))))`:
  - refused: creating a file `HEAD`, a folded `head`, `x/HEAD`, a rename onto `r/HEAD` or onto `s/head`, a hard link
    `t/HEAD`, `cp` to `u/HEAD`, a symlink `y/HEAD`;
  - allowed: a folder `HEAD/` and a file inside it.
- **The broker refuses tracked paths whose last component folds to `HEAD`.** It checks every tree it is about to write
  (`write-tree`, `commit`, `merge-tree`'s result, `update-index` input, a fetched branch it is asked to check out),
  matching the way the volume folds names (case, and the characters git's own path check ignores). So a HEAD file
  can't enter the project's history through the platform. History that already holds one is listed, below.
- **Switch-on and the scan list every existing `HEAD` entry outside a `.git`** in every area the project may write:
  the workspace, the runtime folder, `allow_write`, its scratch roots, and tracked `HEAD` paths in the repo's history
  on its branches. Switch-on refuses until each is gone or the owner accepts it by path. The rule stops new ones; the
  rename case needs the precondition.

### Should-fix

- **S1, refs.**
  - `update-ref` and `symbolic-ref` (and `update-ref --stdin` lines) take only `refs/heads/`, `refs/tags/` and
    `refs/notes/` (and `HEAD` → `refs/heads/…` for `symbolic-ref`).
  - Root refs and pseudo-refs are refused. Measured: `update-ref SHALLOW <commit>` writes `.git/shallow` and makes the
    repo look shallow to the owner; `MERGE_HEAD` git itself refuses.
  - `refs/replace/` is refused. Measured: a replace ref made the owner's `git show main:f` and `checkout -f main` show
    and write content other than the commit's, and a self-replace broke the owner's `status` and `log` ("replace
    depth too high").
  - The broker's git runs with `GIT_NO_REPLACE_OBJECTS=1`.
  - Fetch refspecs are the broker's own, so a fetch can't write `refs/replace/` either.
- **S2, the rebuild.**
  - **The old git dir is read only under the project's own profile, with the exec guard.** Measured:
    - run as the owner, the old config's `fsmonitor` ran on `ls-files --stage`, and was quiet only with the hardened
      `-c`;
    - even with the hardened `-c`, the owner's fetch from the old dir **followed a planted
      `objects/info/alternates` into project B** and copied B's file in;
    - under A's own profile, the same read failed loudly ("unable to normalize alternate object path") and copied
      nothing.

    So the old dir is turned into a bundle under A's profile, and the fresh git dir fetches that bundle under P1, with
    `transfer.fsckObjects`. A planted alternates file stops the rebuild, which reports it. The owner removes it from
    the kept copy and runs again; it is never followed.
  - `info/exclude` and the files under `brunna/` are copied by opening without following links, regular files only,
    with a size cap.
  - **Linked worktrees.** `worktree add --no-checkout` at an existing path fails (measured). So the broker writes the
    admin dir (`HEAD`, `commondir` = `../..`, `gitdir`), then `worktree repair` checks it. Measured: repair, then
    `read-tree` of the branch, left the worktree with its uncommitted file intact.
  - **The order:** refuse while a session is open; stop the project's services and jobs; render the fence for every
    writer; rebuild; start them again.
  - `git-broker restore <project> --at <time>` implies switching the project off, and runs the scan afterwards.
- **S3, pushes as the owner.** A push to GitHub starts Actions with the repo's secrets, on any branch or tag push. So:
  - **Sessions only.** Services and jobs push only through `merge-branch`'s `push_on_merge`, as today.
  - **Branches only, never tags, never the default branch.** Unless the registry says otherwise; the default branch is
    `merge-branch`'s.
  - **A push that changes `.github/workflows/**`** (the diff between the destination's current commit and the pushed
    one) is refused unless the registry allows it.
  - **The broker computes the destination itself.**
    - It pushes to the registry's **URL**, not a remote name, with explicit `<src>:<dst>` refspecs. So `pushurl`,
      `remote.*.push` and `branch.*.merge` in the repo's config never apply.
    - Refused: `--delete`, `:<ref>`, `--tags`, `--follow-tags`, `--mirror`, `--prune`, `--all`, a `+` refspec.
      `--force-with-lease` is allowed only on the project's own non-default branches.
  - The config allowlist drops `remote.*.pushurl`, `remote.*.push` and `branch.*.merge` naming a protected ref.

  Question 1 below.
- **S4, the platform's hooks stay for the owner's own git.** The commit gate's and `commits: conventional`'s hooks in
  `.git/hooks` are kept, written by the platform as today, so the owner's terminal commits still go through them. The
  broker's `-c core.hooksPath=<forwarder folder>` overrides them only for broker git.
- **S5, the socket rule is scoped to the broker's folder.** Measured (probe F):
  - "deny every outbound unix socket, allow its own" also cut a service's unrelated socket (a local database);
  - `(deny network-outbound (remote unix-socket (subpath <broker folder>)))` plus the allow for its own socket kept
    the database socket, refused project B's socket, and allowed its own.
- **S6, nested projects and tool worktrees.**
  - **Nested or overlapping projects.** A project whose nonce folder (`<workspace>/.hub-run/git/`) lies inside
    another project's write roots isn't served. The deepest root claims a folder, as `test-runner.claimant` does,
    so the outer project can't prove the inner one.
  - **Codex app worktrees** (under the user's Codex home, outside the project's folders). By the principle, git in a
    normal Codex app worktree must work, not fail. The design: such a worktree is served when all of these hold:
    - its gitfile leads to an admin dir inside a common dir the project owns, and that admin dir's `gitdir` leads back;
    - no other project's write roots contain the worktree;
    - its nonce folder is the worktree's own `.hub-run/git/`, listed in the repo's `info/exclude` by the platform.

    Identity is then "can write that worktree", which only the session working in it can. The fence there comes from
    Codex's rendered profile, whose writable root that worktree is. Unmeasured: whether the Codex app's sandbox
    writes the worktree only, and where it puts the worktree. Question 3.
- **S7, hook tools inside a commit.**
  - **lint-staged and the pre-commit framework** run `git stash` and `git add` inside the commit's own hook, with
    `GIT_INDEX_FILE` set by git to the commit's in-flight index (`index.lock` or a `next-index-*.lock`).
    - The forwarder gives the hook's environment a token for that commit.
    - The wrapper forwards a call carrying the token, with its `GIT_INDEX_FILE`.
    - The broker accepts that variable only when it names exactly the in-flight index of the commit the token belongs
      to, and runs the call under P1 (`add`), or split (`stash`).
    - Any other `GIT_INDEX_FILE` stays "in place".

    Unmeasured.
  - **The pre-commit framework's own repos** (`git init`, `remote add`, `fetch`, `checkout` in its cache).
    - The platform sets `PRE_COMMIT_HOME` (with the other tool caches in `policy.cache_env`) to a per-project cache
      folder outside every `.git`.
    - The broker accepts `init` there as a recorded scratch root: https URLs only, fetched under P1, checked out by P2.
    - The owner never runs git in it.

    Unmeasured.
  - **The project's own hooks** run through the forwarder in the plain project sandbox when the registry declares a
    tracked hooks folder (the old question 4, settled).
- **S8, a partial checkout fails the call.** A checkout that couldn't write a file exits 0, with only an "unable to
  create file" message (measured: the writer profile's refused `x/HEAD`). The split's own check (`diff-files --quiet`
  on the temp index after P2) turns any such result into a failure, and no ref or index moves. Measured: 40 of 40
  partial checkouts under the race were refused, and 40 of 40 completed without it.

### Nits

- **N1.** Every broker fetch, bundle fetch included, runs with `transfer.fsckObjects=true`. Measured: a crafted tree
  with a `.git` entry was accepted by a plain fetch and refused with it ("hasDotgit"). `hash-object --literally` is
  refused.
- **N2.** The check after a stop reads only `rebase-merge/`. The sequencer that `cherry-pick` and `revert` use refuses
  a todo holding an `exec` line, measured twice: "cannot cherry-pick during a revert", nothing ran. `git am`'s
  `rebase-apply/` holds no commands.
- **N3.** `core.hooksPath` and `core.fsmonitor` stay on the command line (`-c`), where no config file can override
  them. They come off the list of what the broker's global config holds.
- **N4.** `.git/brunna/` stays writable by the project: the outbox, the test runner's store of passes, the locks. So a
  pass recorded there, and the commit gate's verdicts built on it, are the project's word about itself, **not a
  boundary**. The boundary is that the project's code never runs as the owner (r2 B3 of the fence doc) and that its
  git dir is written only by git. `merge-branch` re-runs the tiers when a recorded pass can't be trusted. A
  switched-on project's records count only for the project's own convenience.

### Decided in the design (were owner questions)

- **Other URLs** (old question 2). https only, fetched under P1 (the project's network, no credentials), checked out by
  P2. A path is read under the project's own profile into a bundle. `ext::`, `file://` outside the project's reach,
  and ssh are refused.
- **Scratch repos** (old question 3). Made only by the broker, in the test runner's per-run temp folder, the project's
  own folders, and its pre-commit cache. Recorded per project, and dropped with their folder. `init --bare`,
  `clone --bare`, `clone --mirror` and `--separate-git-dir` are refused (a bare repo is a git-dir shape the owner's git
  would enter).
- **Interactive git** (old question 7). No pty relay. The editor callback runs a caller's editor on its own side
  (order step 8). `add -p` and kin are refused, with the `git apply --cached` way said.
- **Drivers** (old question 10). A driver named per project in the registry runs **through the forwarder in the plain
  project sandbox**, never inside P1 or P2's git: the filter's content is streamed to the broker and back. git-lfs's
  long-running `process` protocol is relayed the same way. Unmeasured (no git-lfs here).
- **`safe.bareRepository`** (the fence doc's question 2). Answered: set by the owner 11-14, now a checked precondition.

### Open questions for the owner (r2: replaces the list above)

1. **Pushing with your credentials** (S3). **Recommended:**
   - sessions only, to the project's own non-default branches;
   - no tags;
   - no change to `.github/workflows/**` unless the registry allows it;
   - `--force-with-lease` allowed there, any other force refused;
   - services and jobs only through `push_on_merge`.
2. **Signing** (unchanged). **Recommended:** not through the broker. Your key isn't used on a project's request.
3. **Codex app worktrees** (S6). **Recommended:** serve them under the conditions in S6, once a measurement shows the
   app's sandbox writes only that worktree. Until then, git there says plainly that it can't run, and why.
4. **Submodules** (unchanged). **Recommended:** none at first.
5. **Switching on** (unchanged). **Recommended:** you flip it per project. The rebuild runs then, with nothing open and
   the project's services stopped.

### Order of work, revised

Step 2 now builds the split (P1/P2, the next-index rename, the check after materialising), with the composed operations
in the B1 table. It does not build a single writer profile. Every composed operation gets the race probe (B/C) and the
B2 probes (a HEAD path through the broker; a renamed folder holding HEAD, with and without the precondition) as
tests, plus the S1 ref probes, the S2 rebuild probes (fsmonitor, alternates, worktree repair) and S5's socket probe.
Doctor's precondition check lands with step 1.

## r3 (11-14), after the second blind review

A second cold review of r2 found that the split held against everything it tried. It found one blocking point (B-1),
eight should-fix items (S-a to S-h) and a nit. Its probes were re-run here, and the measurements below were added (git
2.50.1, the real `service_profile` plus the candidate rules). Where r3 and anything above disagree, r3 holds.

### B-1: the owner's checkout race (this exists today, with or without the broker)

**The finding** (re-run):
- A fenced project process loops, swapping a tracked folder `d/` for a symlink to a folder it can't write itself. The
  stand-in here is a fake home folder, and the project's direct write there was refused.
- The owner's plain `git switch` wrote **2,925** of the tree's files into that folder in the first round. That
  included overwriting a file already there (`a2990`, now "planted 2990").
- `git worktree add`, which is how an agent tool's worktree mode makes a checkout as the user, wrote **2,920** in the
  first round.
- The review also measured `checkout -f`, and found that `checkout.workers` and `core.symlinks=false` don't help.

Aimed at `~` or `~/Library/LaunchAgents`, this is arbitrary writes as the owner, so code runs as the owner at the next
login. No owner action is needed: an agent's own tool makes worktrees as the user while the session's commands run.

**The residual, plainly** (also in "What stays residual" above): during any unsandboxed checkout (the owner's
terminal, editor or git app, or an agent tool's worktree mode), while any process of the project runs (a session's
command, a service, a job), the project can make that checkout write its files anywhere the owner can write.

**A measured partial mitigation: no symlink creation in the workspace.**
- The Seatbelt rule is `(deny file-write-create (require-all (subpath <workspace>) (vnode-type SYMLINK)))`.
- **Refused:** `ln -s` in the workspace; a link made elsewhere and renamed into the workspace; a link renamed onto
  `d`. A folder can't be hard-linked, so the swap needs a link.
- **The race with the rule in the attacker's profile:** 40 rounds, nothing written into the fake home, and the
  existing file intact.
- **What it breaks:** `python3 -m venv` and `uv venv` (both link the interpreter), and npm's `node_modules/.bin` and
  `file:` dependencies (npm exited 255).
- **Carve-outs.** The rule allows link creation inside `<workspace>/.venv/` and inside any `node_modules/` folder; the
  folder entries themselves stay covered.
  - With them, `python3 -m venv .venv`, `uv venv .venv` and `npm install` of a `file:` package with a `bin` worked.
  - A venv elsewhere (`venv2`) was still refused.
  - **A link made inside a carve-out can't be renamed onto `d`** (refused). The rename's destination counts as a link
    creation outside the carve-out. **But a folder holding a link can** (the cold review of the build, 11-14): see "B-1
    as built, after its cold review". The rule was removed on 11-15 (the last section).
  - **The broker refuses tracked paths at or under a carve-out name** (`.venv`, `node_modules`), so no checkout ever
    writes into a folder where links may exist.
  - More carve-outs (other languages' tool folders) are added one at a time with that same refusal, from the refusal
    log.
- **P2 needs an allowance for tracked symlinks.** Under the rule, P2's checkout of a tracked link failed ("unable to
  create symlink"). The allowance is computed per request: exactly the paths where this operation writes a tracked
  link (`(literal <path>)` with the link type). Measured: with the allowance for `tl` only, `tl` was made and the other
  tracked link was still refused.
  - The allowance is P2's alone. P2 runs only git, under the exec guard (r3 S-f), and only while the broker holds the
    index lock (S-e).
  - The broker also refuses, at commit time, tracked links whose target is absolute or leaves the worktree.
- **Coverage: services and jobs only.** Only the platform renders their Seatbelt profile. The agents' own sandboxes
  can't say it, as far as their binaries show (read, not run):
  - **Claude Code:** its sandbox settings take paths only (`filesystem` allow and deny for read and write, `network`,
    unix sockets, `excludedCommands`). The Seatbelt profile it generates uses `vnode-type` only for directory metadata
    reads and `/dev/null`. There is no symlink rule and no setting that makes one.
  - **Codex 0.167:** its permission profiles take filesystem read, write and deny by path or glob, plus network. Its
    Seatbelt generator uses `vnode-type DIRECTORY` only, to pin folders against removal. No symlink rule.

    There was nothing to measure without a setting to try.

  So a session's own commands (the commonest project process) stay able to swap.
- **Claude Code has a `WorktreeCreate` hook** that replaces its git worktree creation: "for another version-control
  system"; the hook prints the path it made. So the platform can make Claude's worktrees itself, through the broker
  (P1 the admin dir, P2 the checkout under the symlink rule's allowance). The Codex CLI binary shows managed worktrees
  in a "configured pool" and no such hook. The Codex app wasn't inspected.

### B-1: the options (the owner's decision)

| option | what it gives | what it costs |
|---|---|---|
| **1. Accept and document** | nothing new; the status page and doctor say it plainly | while any process of a sandboxed project runs, the owner's and the agent tools' checkouts can be made to write anywhere as the owner; "confined" can't be claimed for checkouts; the principle isn't met for them |
| **2. Partial mitigation for services and jobs** (**built, then removed by the owner on 11-15**; the symlink rule, the carve-outs, P2's allowance, the broker's refusals) | services and jobs can't make a link directly in their checkouts; **they can still move in a folder holding one**, so it is friction, not a boundary (the build's cold review, 11-14) | venvs and tool folders outside the carve-outs break for services and jobs, loudly; the carve-out list needs maintaining; **sessions stay open**, so it doesn't close B-1 alone |
| **3. The platform makes worktrees for sandboxed projects** (Claude's `WorktreeCreate` hook → the broker, with P2 under the rule's allowance), and doctor warns about the owner's own checkouts | an agent tool's worktree mode stops being the no-owner-action trigger, for Claude; the owner is told when their checkout is exposed (a project process is running) | Codex has no such hook found; the owner's own checkouts are only warned about, not protected; a hook to build and maintain |
| **4. Never "confined" while any process of the project can run during a checkout** | the confined check stops overclaiming | in practice a sandboxed project's sessions are never "confined" (their commands can always run during a checkout), so D9's goal of a confined Codex session isn't reached by this design |

**Recommended:** 2 and 3 now, and 4 for sessions until a tool's own sandbox can refuse link creation in the workspace.
The platform also asks Anthropic and OpenAI for that setting, which turns 4 off runtime by runtime as each canary
passes. Until the owner decides, the principle (the top of this doc) and Codex's confined check carry the caveat
written there.

### Should-fix

- **S-a, no owner-identity step resolves a path through a folder the project can write.**
  - **`init`.** Measured: the broker checked `<dir>` (inside the project, new) and ran `git init --template=` as the
    owner, while a fenced process flipped `<dir>` between absent and a link to the fake home. **The owner's git dir
    landed in the fake home after 3 calls** (`HEAD`, `config`, `objects`, `refs`). Now:
    - the broker makes `<dir>` through no-follow directory descriptors and takes its real path from the descriptor;
    - `init` runs under a profile whose only write is `<that real path>/.git`, and the project's read rules apply;
    - a swap afterwards makes git write elsewhere, which is refused.

    Not yet measured with the fix (step 2).
  - **Owner network operations** run only on the workspace's own `.git`. That entry is pinned by the fence, and no
    folder above it is project-writable. For any other repo (a worktree's admin dir, a scratch repo), the broker
    fetches into a broker-owned bare repo outside every project folder, and P1 fetches from there through a bundle.
  - **Gitfiles** (worktrees, the rebuild) are written by the broker through no-follow directory descriptors.
  - **P2 makes the temp index copy** into its own temp folder. The broker never writes a temp path a project could
    have pre-made as a link.
  - **The hook forwarder sends contents, not paths.** For the commit gate, it sends the in-flight index's bytes. The
    broker writes them into its own private folder (`hubconf.private_tmp()`) and runs the test from there.
  - **Scratch repos** are recorded by real path plus device and inode of their `.git`. A moved or replaced repo isn't
    served.
  - **The rebuild's worktree repair.** Measured: an old admin dir's `gitdir` aimed at the fake home made the owner's
    `worktree repair` write a gitfile into the fake home, and `worktree list` then listed it as a worktree. Now:
    - worktree paths come only from the registry or the platform's records (the tool worktree folders under the
      workspace, walked without following links), never from the old admin dir;
    - repair runs under a profile that may write only that worktree's `.git` and its admin dir.
- **S-b, P1's and P2's reads are exactly the project's own read rules.** Measured with the review's probe:
  - with P1 built on the project's profile, four reads were refused and nothing of project B's entered the objects:
    `hash-object -w <B's file>`, the same through a folder link, `commit -F <B's file>`, `apply --cached <B's patch>`;
  - with a git-dir writer that had no read denies, each of the four put B's secret into the objects;
  - `add` through a folder link was refused by git itself ("beyond a symbolic link") either way;
  - a hard link of B's file into the project's folder was refused;
  - an https `ls-remote` worked under the project's profile with the exec guard (fetches from https under P1 are
    fine).

  The four become tests. **There is never a profile with the git-dir write and no read rules.**
- **S-c, the self-check after P2.** Measured: with an unrelated local change in `a` (git's own `switch` carries it),
  `diff-files --quiet` on the temp index failed the call. By then P2 had already rewritten `b`, HEAD stayed, status
  showed both changed, and a retry was refused ("b not uptodate"). Now:
  - the check covers only the paths this operation changes (`diff-tree` old → new, plus conflict paths), compared with
    the dirty set taken before the operation;
  - on failure, P2 puts back what it wrote: `read-tree -m -u <new> <old>` on its temp index, for those paths only.

  Not yet measured with the fix.
- **S-d, pushes.**
  - **Services and jobs can't be told from sessions by the nonce**, since both can write the project's folder. So they
    get **their own socket**: their Seatbelt profile allows only it, and the broker refuses pushes on it. Sessions use
    the session socket. Codex sessions reach any socket, but a service can't reach the session socket.
  - **`push_on_merge: true`:** `merge-branch` runs in the session, so its push reaches the broker like any session's
    push. Allowing it means **any session of that project can push the default branch**: its checks run in the session
    and can be skipped.
  - **The CI rule is widened to `.github/**`.**
  - Question 1 says what a push means: **every push runs this project's code in CI, with whatever secrets your
    workflows give it.**
- **S-e, locking.** The broker takes `.git/index.lock` (created exclusively, as git does) at step 1 and holds it to
  step 4. P1 computes the next index into it, and step 4 renames it over `index`, which is how git commits an index.
  So the owner's concurrent git waits or fails as with two terminals. Worktree-writing operations run one at a time
  per worktree.
- **S-f, the composed set is the allowlist.**
  - There is never a combined profile: P1 writes only the git dir, and P2 only the worktree.
  - The allowlist of forwarded operations is exactly the composed set. Everything else is refused, with its reason.
  - **In place** (real git in the project's sandbox; they write no git dir): `clean`, `archive` (local, to stdout),
    `check-ignore`, `check-attr`, `shortlog`, `diff-tree`, beside the read forms above.
  - The check after a stop aborts by composition (fixed above).
  - **Compositions planned for the everyday operations:**
    - `--abort` (rebase, cherry-pick, revert, am): P1 puts the refs back and sets the index to that tree, removing the
      state folder; P2 resets the worktree.
    - `merge --abort`: like `reset --merge`; P2 does `read-tree -m -u` from the current index to HEAD, so unrelated
      local changes stay.
    - `--continue`: P1 commits the current index with the saved message, then the next step. The state is written in
      git's own format with no `exec` lines, so the owner's own git can continue it too.
    - `--skip`: P1 drops the step, P2 resets to HEAD.
    - `restore --source <rev> <path>` and `checkout <rev> -- <path>`: P1 sets the next index for those paths
      (checkout only); P2 runs `checkout-index` from a temp index holding `<rev>`'s entries.
    - `stash apply`: as pop, without the drop.
    - `merge --squash`: the merged tree into P1's next index, no commit, `SQUASH_MSG`; P2 materialises.
    - `am`: each patch through `apply --cached` into P1's next index plus `commit-tree`; P2 materialises. A 3-way `am`
      is refused at first.

    Each gets the race probe and the S-c check as tests.
- **S-g, Codex app worktrees.** They sit under the user's Codex home. Every sandbox denies that folder, because it
  holds every conversation and the login. Serving one means carving that worktree out of that folder for the project's
  sandboxes. That is the trade in question 3. The Codex CLI's managed worktrees have a "configured pool"; if it can be
  placed inside the project, the carve-out isn't needed. To check.
- **S-h, nested projects** aren't served, unless every rendering of the outer project (Seatbelt, Claude, Codex) denies
  writes into the inner one. The review measured the outer project writing the inner one's folder by default.

### Nit

The hook forwarder runs under `python -I`, with every `PYTHON*` variable stripped, or as a compiled helper. Nothing
from the project's environment shapes the program the exec guard lets run.

### Open questions for the owner (r3: replaces both lists above)

1. **B-1's options.** **Recommended:** 2 and 3 now; 4 for sessions until a tool's own sandbox can refuse link
   creation in the workspace, and the platform asks Anthropic and OpenAI for that setting.
2. **Pushing with your credentials.** Every push runs this project's code in CI, with whatever secrets your workflows
   give it. **Recommended:**
   - sessions only (a separate socket for services and jobs, where push is refused);
   - the project's own non-default branches; no tags;
   - nothing under `.github/**` unless the registry allows it;
   - `--force-with-lease` there, any other force refused.

   Keep `push_on_merge` off for sandboxed projects: with it on, any session of the project can push the default branch.
3. **Signing.** **Recommended:** not through the broker. Your key is never used on a project's request.
4. **Codex app worktrees.** Serving one means carving it out of your Codex home, which every sandbox denies because it
   holds every conversation and your login. **Recommended:** don't carve. Git in such a worktree of a sandboxed project
   says why it can't run, and points to a worktree inside the project (Claude's mode, or the Codex CLI's pool if it can
   be placed there). Revisit once Codex can keep worktrees in the project.
5. **Submodules.** **Recommended:** none at first.
6. **Switching on.** **Recommended:** you flip it per project. The rebuild runs then, with nothing open and the
   project's services stopped.

### Order of work, revised again

- **Step 1** gains the symlink rule for services and jobs, with the carve-outs, behind the per-project switch (if
  option 2 is chosen), and doctor's B-1 row.
- **Step 2** gains:
  - S-a's real-path and descriptor discipline, with the `init` race and the repair probe as tests;
  - S-b's four read probes; S-c's dirty-set check and its rollback; S-e's lock;
  - S-f's compositions;
  - P2's per-request link allowance.
- **Step 4** gains Claude's `WorktreeCreate` hook (if option 3 is chosen), with the owner race probe run against a
  worktree made through it.

### B-1 as built (11-14): mitigations 2 and 3

**The owner's decision (11-14):** "Yes, build 2+3", for B-1, independently of the broker (which isn't built). Both are
built for every sandboxed project, with no switch. Option 4 (sessions never "confined") and question 4 (Codex app
worktrees) are still open. Until they are decided, the caveats on the principle and on Codex's confined check stand.

**Mitigation 2: a sandboxed project's services and jobs can't make a symbolic link directly in their checkouts.**
Withdrawn as a protection by the build's cold review: a folder holding a link can still be moved in (next section).
- **The port.** `policy.links` is neutral:
  - the roots are the workspace and the runtime folder (a deploy's `git pull` there is a checkout too);
  - the tool folders `LINK_FOLDERS` may hold links: `.venv/` at a root's top, and `node_modules/` at any depth, inside
    the folder only, never the folder's own entry;
  - so may the platform's own test runs (`.brunna/test/`, both spellings), where the test runner checks out tracked
    links and tests make links in their temp folder;
  - so may the tool caches `cache_env` names (`.git/brunna/cache/`, both spellings), which no checkout writes;
  - `allow` lists exact paths one platform job may link.
- **The Seatbelt rendering.** `seatbelt.link_rules` sits in the isolation both profile kinds share. That covers
  `sandboxed-run` (services, command jobs, the test runner's wrap, the platform's git work run as the project) and
  cap-run. It is placed before the control-file rules, so those still win. An allowance outside the roots is dropped.
  The guarantee is `links-blocked`, probed by `tests/test_sandboxed_services.py`'s Conformance.
- **Linux** doesn't declare it, and doctor says so (a warning row: "the platform doesn't refuse links on this OS").
  Contrary to the r3 assumption, Landlock does have a make-symlink right. But it is granted per folder that exists when
  the rules are made, so a `node_modules` or `.venv` made later would get no links. Unmeasured, and not built.
- **Exact allowances, for the platform's own links made as the project:**
  - `worktree-memory.sh` (`project_git.py run-as --allow-link`): a worktree's `.agent-memory` link and the rules-file
    links;
  - the worktree maker below: each tracked link of the checkout.
- **Carve-outs, measured before adding any:**
  - **uv's cache needs one.** With the caches where `policy.cache_env` puts them, `uv pip install six` and `uv sync`
    failed under the rule: uv links each wheel to its unpacked archive in the cache. `uv python install 3.12` puts
    links in `uv-python`. With `.git/brunna/cache/` carved out, all three worked. (`uv venv` alone made no link in the
    cache: that first measurement was too narrow.);
  - a pnpm store in the workspace held no links (its links are in `node_modules`);
  - cargo's `target/` couldn't be measured: in the sandbox rustup tries to download a toolchain and can't reach the
    Keychain to check certificates;
  - tox and pdm (`.tox`, `__pypackages__`) aren't installed here.

  Only the cache folder was added. A tool that needs another fails loudly, and gets a folder added after it is measured.
- **Before it goes live on a machine,** list each sandboxed project's declared services and jobs and what they run,
  and look for link-making outside the carve-outs (`ln -s`, `symlink`, a venv outside `.venv`). The deployment this was
  built on was checked so, read only; what was found is in the session's report, not here.

**Mitigation 3: Claude Code's worktrees in a sandboxed project are made by the platform, as the project.**
- **The contract**, read from Claude Code's binary (2.1.x, not run):
  - a `WorktreeCreate` hook replaces its own git worktree creation;
  - its input is the common fields plus `name`;
  - the path is read from the last line of stdout (a relative one against the session's folder);
  - a non-zero exit or no path fails the worktree;
  - `WorktreeRemove` (`worktree_path`) replaces its removal.
- **What renders the hooks.** `render-project-settings` writes both hooks into every sandboxed project's settings
  (`runtime_claude.platform_hooks`). Each hook runs `hub-event --runtime claude worktree-make|worktree-drop`, ending in
  `exit 1` and never in `|| true`: if the platform can't make the worktree, it isn't made, and never as the user.
- **The new port.** `worktree_maker` is an optional port of `runtime_ports`: `worktree_request`, `worktree_location`,
  `worktree_reply`, `makes_worktrees`.
- **How the worktree is made.** `session_worktree.make` runs `project-git-job.py session-worktree` as the project,
  through `project_git`, inside its sandbox:
  - the place is `<workspace>/.claude/worktrees/<name>`, on branch `worktree-<name>` (new from HEAD, or the existing
    one);
  - the steps are `worktree add --no-checkout`, `reset`, then `checkout-index`;
  - the tracked links are allowed by exact path, only relative ones without `..` under the new worktree (the list is
    the project's own word, read from its repo);
  - a checkout that fails takes the worktree and its new branch back, so the same name can be made again;
  - tracked files the sandbox may not write (control names, a runtime's own folder) are left out as skip-worktree, so
    git doesn't show them as deleted, and the hook says so on stderr.

  A swap during that checkout can only redirect writes to places the project could already write.
- **What changes:** a sandboxed project that tracks files under control names (skills folders, say) gets Claude
  worktrees without them (left out). Doctor already lists such files. The hooks carry a 30-minute timeout, past the
  checkout job's own.
- **Going live:** the hooks reach every sandboxed project's settings at the next render after the merge (every 15
  minutes). From then on, Claude's worktree mode there goes through the platform, and fails closed if it can't. So the
  real-Claude check below comes before `merge-branch`.
- **The existing worktree path, checked.** For a sandboxed project, `worktree-memory.sh` already runs its git and
  links as the project (`project_git.py run-as`), and `render-project-settings --worktree` writes the worktree's
  settings without following links. The remaining owner-run checkouts in a sandboxed project's folder are Claude's own
  (now handed over), the Codex app's and Codex CLI's managed worktrees (left as they are: question 4), and the owner's
  own tools. `propose` is off for sandboxed projects, and its copy is outside the workspace.

**Doctor:**
- one row, a warning either way: services and jobs can't make a link directly but can move in a folder that holds one,
  "friction, not a boundary" (macOS), or the platform doesn't refuse links on this OS (Linux);
- per sandboxed project, a warning: while its programs run, a checkout the owner makes in its folder (terminal,
  editor, git apps, the Codex app's worktrees) can be made to write its files anywhere the owner can write; plus which
  agents' worktrees the platform makes there (read from the project's settings);
- per project, files tracked under a tool folder (a carve-out a checkout of the owner's could still be redirected
  through).

**Tests** (`tests/test_link_fence.py`, and the Conformance probe):
- the port and its rendering; the order before the control rules; Seatbelt declares the guarantee and Landlock
  doesn't;
- under Seatbelt:
  - refused: a link in the workspace or runtime folder, one moved in from another writable folder, one made in a tool
    folder and renamed onto a tracked folder's place, a tool folder that is itself a link;
  - allowed: links inside `node_modules` (nested too), `.venv` (top only) and a test run;
  - an exact allowance; the positive control without the rule;
  - venv, `uv venv` and npm's `.bin` links still work;
- **the owner race** (the review's `owner_race2`, the attacker under the rule): nothing written outside, the file there
  intact, the link never made. The positive control here only shows the link can be made without the rule; the race
  landing without it (~2,900 files in the first round) was measured in scratch, not in the test;
- `--allow-link` end to end through `sandboxed-run` (the Conformance probe), and `project_git.py run-as`'s parsing;
- the doctor rows (macOS, Linux, a tracked file in a tool folder);
- the hook contract (request, location, reply, `makes_worktrees`, the commands fail closed and are the platform's);
- `hub-event` exit codes and stdout;
- a worktree made through the hook under the real Seatbelt profile: made as the project, the tracked link present, a
  control file left out as skip-worktree, status clean, removed again, nothing run for an unsandboxed project or a bad
  name;
- a race against the hook path writing nothing outside, its attacker under no link rule (as a session's own command
  would be);
- a failed make leaves no worktree or branch behind; tracked-link paths outside the new worktree are dropped.

**Needs a real Claude run** (the platform's own tests can't start Claude; listed for the owner or the coordinator):
1. In a sandboxed project, `claude --worktree <name>` (and the `EnterWorktree` tool, and a subagent with
   `isolation: worktree`) runs the `WorktreeCreate` hook from the project's `.claude/settings.local.json`, opens the
   session in the path it printed, and makes no worktree with its own git. To check: no `git worktree add` from
   Claude's process, the branch `worktree-<name>`, `hub-event.log`'s `worktree-make` line.
2. On exit, choosing to remove the worktree runs `WorktreeRemove`, and the folder is gone.
3. A hook that fails (a name the platform refuses, say) shows Claude's "WorktreeCreate hook failed" and makes no
   worktree.
4. Whether a project's local settings are trusted for this hook in a fresh folder (the binary mentions "workspace not
   trusted"), and that `CLAUDE_CODE_RESTRICT_PERSONAL_CONFIG` doesn't apply to project-level hooks.

### B-1 as built, after its cold review (11-14)

A cold review of the build found that mitigation 3 holds, and that mitigation 2 is bypassed (blocking). Where this
section and the ones above disagree, this one holds.

**Mitigation 2 is friction, not a boundary.**
- **The bypass:** `mv d d.x; mv X d`, where `X/sub` is a link. The link can be made in a tool folder (`node_modules`),
  in the temp area, or earlier by a session, whose own commands no rule covers. Seatbelt checks a folder's rename as a
  directory's creation, which mkdir also is, so the link rule never sees it. The review's owner races, run under the
  rule, wrote 660 to 717 files outside the project and overwrote an existing file there.
- **Is there a direct rule? Measured here:**
  - denying directory creation in the roots (`file-write-create` with `vnode-type DIRECTORY`) refuses the moved-in
    folder, and mkdir too: unusable;
  - denying a directory's removal outside the roots (a rename's source) refuses the folder staged in the temp area,
    and `rmdir` there too; it leaves a folder staged inside the roots (in `node_modules`, or by a session) free to move;
  - no Seatbelt filter was found that tells a folder's rename from mkdir.
- **So the claim is withdrawn everywhere.** The guarantee's text (`isolation.py`), `seatbelt.link_rules`'s docstring,
  doctor's row (now a warning) and this doc all say: the rule refuses making a link directly in the workspace, not
  moving in a folder that holds one. **B-1 stays open for services and jobs too.**
- **The code stays until the owner decides whether to keep it.** A test (`test_link_fence`, "the limit") asserts that
  the folder's rename lands. It fails the day the limit is gone, so the doc can be changed then.

**Mitigation 3 holds; its should-fix items, built:**
1. **The tracked links come from the branch that is checked out.** A new op, `worktree-plan`, lists them from
   `refs/heads/<branch>` when that branch exists, else HEAD, in the same call that says whether it exists. The review
   reproduced "unable to create symlink … Operation not permitted" when they came from HEAD.
2. **Timeouts, and nothing left behind.**
   - Claude Code's default hook timeout reads as 600 s in its binary. The plan (60 s), the job (360 s) and the undo
     (60 s), plus a margin, make `HOOK_TIMEOUT_S` = 540 s, which is the hooks' `timeout`.
   - A make that doesn't finish is recorded in the platform's state (`.state/session-worktree/<project>/`) before the
     job starts. A failure or the job's own timeout undoes it at once (`session-worktree-undo`, as the project: the
     worktree forced out, pruned, and the branch deleted only if this make created it).
   - A hook killed by the tool leaves the record. The next make of that name undoes it first. A folder no record names
     is never undone.
3. **The hook-path race test** now runs its attacker under no link rule, against half the names. It asserts that
   nothing is written outside and that at least one make went through while the attacker ran.

**Nits, built:**
- **Names with `/`,** as `EnterWorktree` takes them. Each segment is `[A-Za-z0-9._-]`, with no `..` and no leading dot,
  at most 100 characters in all. The adapter maps them as Claude Code's own worktree mode does (read from the binary):
  `/` becomes `+` in the folder and the branch (`feat/x` → `.claude/worktrees/feat+x`, `worktree-feat+x`).
- **Claude Code's post-create steps** (read from its binary, to be confirmed by a real run):
  - With the hook, every worktree path takes "Created hook-based worktree" / "Using worktree via hook". This covers
    `--worktree`, `EnterWorktree` and agent isolation.
  - It refuses a hook path with dot segments, or one with a link in a component below the repo root.
  - It checks that the path is a folder. For an agent worktree it runs its own `git rev-parse HEAD` there (a read).
  - It does **not** run the git path's setup:
    - the `.worktreeinclude` copy;
    - the `settings.local.json` copy;
    - "Configured worktree to use hooks from main repository" (which sets the worktree's hooks path);
    - `symlinkDirectories`;
    - `sparsePaths`.

    Those run only in the branch where its own git makes the worktree. A sandboxed project's worktree settings are
    written by `render-project-settings --worktree` at session start (`worktree-memory.sh`), as before.

**To look for in the real Claude run** (Claude's debug log, `claude --debug` or `~/.claude/debug/`, and `git worktree
list`):
- "Created hook-based worktree at: <ws>/.claude/worktrees/<name>" (or "Using worktree via hook" for
  `--worktree --tmux`);
- the absence of all of these:
  - "Created worktree:";
  - "Copied settings.local.json to worktree";
  - "Copied N files from .worktreeinclude";
  - "Configured worktree to use hooks from main repository";
  - "Symlinked … from main repository to worktree";
- `hub-event.log`: a `worktree-make` line with the project, and on removal `worktree-drop`;
- for a name with `/`: the folder `a+b` and the branch `worktree-a+b`;
- no `.state/session-worktree/<project>/*.json` left after a successful make;
- whether the hook's own `timeout` (540) is honoured: a slow checkout past 10 minutes is cut either way.

### B-1: mitigation 2 removed (11-15)

**The owner's decision (11-15):** remove mitigation 2, the link rule for sandboxed projects' services and jobs; keep
mitigation 3.

**Why:** the build's cold review showed it isn't a boundary. A folder holding a link can be moved onto a tracked
folder's place, because Seatbelt checks that rename as a directory's creation, the same check as mkdir. No direct rule
was found that closes this without refusing mkdir or rmdir (measured, previous section). A rule that only adds friction
costs every sandboxed project its links outside the carve-outs, and a carve-out list to maintain, while protecting
nothing.

**What went:**
- the policy port `policy.links` and its carve-outs (`.venv/`, `node_modules/`, the test runs, the tool caches);
- Seatbelt's `link_rules` and the `links-blocked` guarantee, with its Conformance probe;
- `--allow-link` (`sandboxed-run`, `isolation.wrap`, `project_git.py run-as`) and its use in `worktree-memory.sh`;
- the link allowances in mitigation 3's job: with nothing refusing links, the worktree job checks tracked links out as
  git does. `worktree-plan` now says only whether the branch exists, for the undo;
- doctor's machine row about links, and its row about files tracked in tool folders.

**What stays:**
- Mitigation 3 whole: the hooks, `session_worktree`, the record and undo, the timeouts, names with `/`.
- Doctor's per-project warning.
- The measurements above, as the record of why.

**What is true now** (`tests/test_session_worktree.py`, `B1Open`): a sandboxed project's services and jobs can put a
link at a tracked folder's place, directly or as a folder holding one. **B-1 is open for them as for sessions.** If that
test ever fails, something refuses links again, and this doc should say so.

### B-1 mitigation 3: the drop, after the real check (11-15)

**The real check** (a sandboxed project on this machine, 11-15, on main after the merge):
- `claude -p --worktree <name>` ran the platform's `WorktreeCreate` hook. The session ran in
  `.claude/worktrees/<name>` on `worktree-<name>`, and Claude made no worktree of its own.
- **The drop failed.** Git's `worktree remove`, run as the project, unregistered the worktree, then couldn't delete
  its folder. In it were the platform's settings (`.claude/settings.local.json`, written at session start) and the
  rules-file link (`CLAUDE.local.md`). Every sandbox protects those names at any depth, and protects a worktree's own
  `.claude` too. So the folder and the branch were left orphaned.

**Fixed:**
- `session_worktree.drop` first removes what the platform itself writes into a worktree, as the platform:
  - which entries: the installed runtimes' local-only files (the instructions port's `local_ignores`; for Claude Code,
    `CLAUDE.local.md` and `.claude/settings.local.json`), and their folder when that empties it;
  - how: walked down from the workspace through no-follow directory descriptors (`nofollow`), files and links only,
    never a folder's contents;
  - which worktree: only one at the tool's place, with a `.git` pointer file read the same way.
- Then the rest goes as the project (`session-worktree-remove`): everything but `.git`, bottom-up, links removed as
  links. **Only when nothing is left** is it unregistered (`worktree remove --force`), and its branch removed with
  `branch -d` (merged ones only). If anything can't be deleted, the worktree stays registered, and the call fails
  saying what is left.
- **The make's undo** clears the platform's entries the same way, then the same removal.
- **The tool's worktree folder** (`.claude/worktrees`) is made by the platform the same way before the job. The
  project's sandbox holds the tool's own folder (`protect_roots`), so a first worktree could not make it.
- **Tests** under the real Seatbelt profile, with the tool's folder and the control names held as in a real project:
  - a drop after a session start wrote the settings and the links: nothing left on disk or registered, and the link's
    target untouched;
  - a folder that can't be emptied stays registered;
  - a drop of anything not at the tool's place is refused.

**Session start writes into a hook-made worktree as the owner** (`worktree-memory.sh` → `render-project-settings
--worktree`). That write goes through `nofollow.replace` from the workspace, folder by folder without following a link.
The worktree's path is resolved first and must stay inside the workspace, so a planted link can at most move the write
to another folder of the same workspace, never outside it. The links and the rest (`.agent-memory`, the rules file)
are made as the project (`project_git.py run-as`). No other owner write was found there.

## Step 1 as built (11-15)

**The owner's decision (11-15):** the design is approved with r3's other five recommendations (questions 2–6: push
policy, signing, Codex app worktrees, submodules, switching on per project). Step 1 is the fence in the policy port and
its Seatbelt rendering, behind the switch. Claude's settings and Codex's profile stay in step 4, as the order of work
says. **No project is switched on.**

**The switch.** `enforce.git: platform` in the registry (`registry.validate_enforce`): the only value is `platform`, and
it needs `enforce.sandbox: true`. It sits under `enforce`, which is in `PLATFORM_OWNED`, so a manifest.update that
changes it is returned (tested). It is in both project schemas (a field added, the version kept). It is refused for
a project in `service_sandbox_exempt`, whose services and jobs get no sandbox at all.

**The port** (`policy.py`, neutral):
- `git_platform(doc)` is the switch.
- `git_fence(doc, roots)` is `None` unless the switch is on. Otherwise it returns:
  - `names`: `.git`, the entry and everything below it;
  - `files`: `HEAD`, any entry but a folder (r2 B2, verbatim: a folder named `HEAD` is the one exception the
    measurements give);
  - `repo_dirs`: `<root>/.git/<platform folder>` for the workspace and the runtime folder, both spellings;
  - `markers`: uv's `sdists-v*/.git` under each repo dir's `cache/uv` (where `cache_env` puts uv's cache).
- `policy()` carries it as `git_fence`.

**The Seatbelt rendering** (`seatbelt.git_fence_rules`, in `_isolation`). So it reaches every profile the platform
applies: services, command jobs, the test runner's wrap, the platform's git work done as the project
(`sandboxed-run`), and cap-run (`seatbelt.profile`). The rules are all denies, placed after the writable allow:
- no `.git` entry, nor anything below one, anywhere, except under each repo dir. The repo dir is kept out of the deny
  with `require-not (subpath …)`, never allowed back: a later allow would also reopen the control-file denies
  (`protect_denies`, `root_denies`) below it;
- no `HEAD` that isn't a folder, anywhere, and no folder named `HEAD` removed or renamed (the swap, below);
- below each repo dir, no `.git` except uv's marker, matched exactly and only as a regular file;
- the repo dir itself is never made from inside, nor removed or renamed; the platform makes it (the rebuild, step 2);
- each folder on the way to the marker's bucket can't be removed or renamed (`file-write-unlink`, as a folder), nor
  made as a link. Moving the bucket out would carry the marker with it.

With the switch off, it adds nothing at all, not even a comment.

**Paths below the `.git` are never resolved** (`seatbelt._fence_spellings`). The profile is computed on every start, and
the first build resolved every path of the fence. A link at the platform folder's unused spelling (`.git/agent-hub` →
`.`), or at its `cache/`, made where nothing fenced it, would then have put the git dir itself into the next run's
exception (the cold review of the build, 11-15; the test fails against that version). Now only the folder holding the
`.git` is resolved, and the rest is written as the policy names it. Such a link changes no rule, and a write through it
is matched in the git dir where it lands, and refused (tested, with the link made outside the fence before the run).

**The swap (the second cold review of the build, 11-15).** `renamex_np(RENAME_SWAP)` is checked with each end's
existing type. So a fenced writer made a folder `HEAD/`, swapped it with a regular file, and had a `HEAD` file anywhere.
Chained with the marker's exception (which had no type then), it built a git-dir shape and moved it onto
`.git/brunna/cache/uv/sdists-v1/.git`. The owner's git, `safe.bareRepository=explicit` included, entered it from a
folder below and ran its fsmonitor. Fixed by two rules, each of which breaks the chain alone:
- no folder named `HEAD` is removed or renamed from inside (`file-write-unlink` with `vnode-type DIRECTORY`; a swap
  is checked as that at both ends). **The cost:** a fenced writer can make a folder named `HEAD`, but can't remove or
  rename it;
- the marker's exception holds only for a regular file, so a folder or a link moved onto its name is refused.

The chain is a test: it lands without the fence, git run outside enters it with the precondition set, and under the
fence it is refused and leaves nothing.

**A missing repo dir** can't be made from inside, as a folder or as a link, under either spelling (before, a fenced
writer could make the unused spelling and then not remove it). A link made there by anything else gains nothing
(above).

**Linux.** Landlock doesn't declare the new guarantee. `isolation.NEEDS` / `unmet()` refuses a policy that carries the
fence on an adapter without it: `sandboxed-run` exits 78 before anything runs, and `exec_service` raises. So a
switched-on project's services and jobs don't run there at all, never unfenced.

**The guarantee** `git-dirs-fenced` is in `isolation.GUARANTEES` and declared by Seatbelt. The Conformance probe
switches its fixture project on wherever the adapter declares it, and checks that `.git` and a `HEAD` file can't be
made in the workspace.

**Doctor** (`doctor_git_platform`, shown when any project is sandboxed):
- the precondition, read with `git config --global --get safe.bareRepository` and never written:
  - `ok` when it is `explicit`;
  - otherwise `warn`, or `bad` once a project is switched on, with the one command that sets it;
- per switched-on project, always `bad` until the broker exists:
  - in `service_sandbox_exempt`: no sandbox, nothing fenced;
  - its services and jobs aren't run here, with the reason (the adapter can't fence, or this machine can't sandbox at
    all);
  - without the precondition: not fenced;
  - otherwise: the broker isn't built, so git's own writes from its services and jobs, and the platform's git work
    done as the project, fail; its sessions aren't fenced either. The fix is to switch it off.

  A project is never said to be fenced whole. The "run isolated" row names `git-dirs-fenced` only once a project is
  switched on.

**Measured** (`tests/test_git_dir_fence.py`, under the real service profile and the real cap-run profile, with real
git). Every refused probe also runs without the fence as its positive control, and lands there. The platform folder's
probes run under both spellings.

Refused under the fence:
- writing `.git/config`, `git config`, a hook, `info/attributes`;
- a nested `.git` (also `.GIT`, `.Git`), a gitfile;
- `git init` in the workspace and in the temp area; `git init --bare` in temp (by the `HEAD` rule);
- a folder holding `.git` built in temp and moved in (refused where it is built);
- `HEAD` as a file, folded (`head`), in temp, as a symlink, a hard link, a rename's destination, a copy, a swap with a
  folder named `HEAD` (in the workspace and in temp);
- `mv .git`;
- hard links to `.git/config` from the workspace and from the platform folder;
- a symlink in the platform folder, written through;
- the platform folder renamed or removed; a `.git` below it;
- a linked worktree's gitfile rewritten;
- `git commit`;
- uv's bucket, its `uv/` folder or its cache moved out; a `.git` elsewhere in the cache; the marker hard-linked as a
  `.git`; the marker as a folder or a link; a folder moved onto the marker's name;
- the platform folder (either spelling) made from inside, as a folder or a link; its `cache/`, `uv/` or bucket made as
  a link.

A run of all of them together left no `.git`, gitfile or `HEAD` file behind.

Still working under the fence:
- `git status` (no `index.lock` left), `log`, `diff`, and status and log in the linked worktree;
- the outbox in the platform folder, and `mkdir -p` of both spellings when they exist;
- uv's marker, under both spellings;
- a folder named `HEAD` with a file in it;
- `.gitignore`, `.gitattributes`, `.github/`;
- the test runner's export, with the index and the object folder in temp.

**The limit (a test pins it, "Limits").** A rename is checked only at its two ends. So a folder already holding a
`.git` or a `HEAD` file is moved into the workspace whole, if it was made outside every fence: by the owner, by another
project (the temp area is shared), or by a session of this project (sessions aren't fenced until step 4). The fence
keeps a fenced writer from building one; it can't stop one that exists from being moved.
- The `HEAD` shape is what the precondition is for (r2 B2).
- A nested `.git` needs every writer of the project fenced, so it is fully closed only after step 4. Even then, a
  `.git` that another project or the owner stages in the shared temp area can be moved in. That is the same exposure
  as that writer's own nested repos.

**Covered by the rule text only:** the runtime folder's repo dir. Real Seatbelt runs cover the workspace, under both
profiles.

**Live check (11-15, this machine).** Each live project's renderings were compared before and after the change: Claude
settings and env, and the Seatbelt service and cap-run profiles. All were byte-identical, and so was
`render-project-settings --report`'s output. That command already exited 1 before the change, for one project's
unrelated drift.

**What the next steps need from this one.**
- **The switch is not safe to turn on yet.** Under the fence, these all fail for a switched-on project:
  - git's own writes from its services and jobs;
  - the platform's git work done as the project (`project_git`'s write ops: `hook-write`, `hook-remove`,
    `set-origin`, `exclude`, `worktree-add`, `worktree-remove`, the memory sync's commit);
  - mitigation 3's `session_worktree` (`worktree add` as the project);
  - `worktree-memory.sh`'s git as the project;
  - the hook installer;
  - `with-lock.sh`'s lock folder, and `merge-branch`'s lock, in the git dir;
  - the test runner's object writes into the repo.

  Steps 2, 3 and 5 move these; until then nobody should switch a project on, and doctor shows a switched-on project
  red. The registry doesn't refuse the switch (except for an exempt project). Refusing it while a session is open,
  and the rebuild (which also makes the platform folder), are step 2's.
- **For the broker (step 2):**
  - `policy.git_platform` is the test for "switched on";
  - `git_fence(doc, roots)["repo_dirs"]` names the folders that stay the project's;
  - P2 (the materialiser) is exactly `service_profile` with the fence;
  - P1 (the git-dir writer) is a new profile built from `_isolation`'s reads, with the git-dir write and the denied
    names. It must not reuse the fence, and must never be a profile with the git-dir write and no read rules (r3 S-b).
- **For the runtimes (step 4):** render the same port into Claude's settings and Codex's profile, each with its canary.
  Until then doctor says sessions aren't fenced.

## Step 2 build list (11-15)

Step 2 is "the broker and the wrapper" ("Order of work" item 2, with r2's and r3's revisions). This is its build list,
in four slices. Each one can be merged and tested on its own, in this order: **a**, then **b**, then **c** and **d** in
either order (both need a and b). Nothing here is built. It changes no owner decision: where the sections above
disagree with each other or with the code, "Open points" at the end says so, and marks the ones that need the owner.

### r1 of the build list (11-15), after a blind review

A cold review of the first version found one blocking point (K1), twelve should-fix items (S1–S12 below, the
review's own numbering, not r2's S1–S8) and nits. All are folded into the slices; where this list and the slices
disagree, the slices hold.
- **K1 (blocking), P2's index copy in the shared temp area.** Every sandboxed project may write `/private/tmp` and
  `/private/var/folders`, so project B could replace A's copy between the copy and `read-tree -m -u`, and steer writes
  and deletes in A's worktree (the rollback used the same copy). Now P2's working folder is in A's own platform
  folder, which only A can write, and the broker writes the copy there through no-follow descriptors (slice b; open
  point 3). A test has a second project try the swap, with the old layout as the control.
- **S1, `init` under P1.** P1 denies `config*` and pins `objects`, `refs` and `info`, so it can't run `git init`. An
  init profile is added (slice a).
- **S2, git state that no plumbing writes.** `MERGE_HEAD`, `MERGE_MSG`, `SQUASH_MSG`, the `rebase-merge/` files, and a
  worktree's admin files (`commondir` and `gitdir` are denied to P1) are written and removed by the broker as the
  owner, through the git dir's no-follow descriptor, with content it generates (slice b; the owner's touches in the
  global constraints). `worktree repair` "only as a check" proved nothing: the broker now reads the gitfile and the
  admin `gitdir` back through descriptors and compares (slice c; open point 8).
- **S3, where bundles live.** P1 can't write the temp area, so every bundle goes out on stdout into
  `hubconf.private_tmp()` (as `project_git.sync_memory` does), and a P1 that reads one gets that single file as
  readable for that call. Never in the shared temp area (slices a, b, c, d).
- **S4, the environment of the broker's git.** It is built from scratch, never inherited (slice a), with a test that a
  canary `http.extraHeader` in the broker's own environment never reaches the server.
- **S5, the service's way to the session socket.** The deny covers `<state>/git-broker/` whole. A test checks that a
  project's own service can't reach its session socket directly, through a link, or through a hard link (slice a).
- **S6, nested projects.** The first version had the rule backwards. Now a project isn't served when another
  sandboxed project's write roots contain its nonce folder (slice a).
- **S7, scratch repos in the shared temp area** could be swapped between the check and P1's run, contrary to "What
  stays residual" (lines 561–562). Now they are served only in folders this project alone can write (slice b).
- **S8, a `HEAD` path through slice a.** `commit` and `apply --cached` are forwarded in slice a, so B2's tree check
  moves into slice a, with the `x/HEAD` probe.
- **S9, the rebuild was untestable as specified.** It now has an in-process seam for tests, a test that the command
  has no bypass, and a `--check` mode that can't write (slice c).
- **S10, the `.github/**` check and "own branches".** The pushed commit's `.github/` is compared with the default
  branch's current tip, not a merge base. "The project's own non-default branches" is defined (slice d; open point 16).
- **S11, tracked control files.** A checkout that would write a file no sandbox of the project may write (the runtime
  control files) would fail partway under P2. It is refused before anything moves (slice b).
- **S12, the race's full run.** Slice b's done criterion now includes one recorded 40-round run per worktree-writing
  operation (open point 21 keeps the question of the `full` tier).
- **Nits:**
  - the rebuild reads the old config with `O_NONBLOCK|O_NOFOLLOW`, a regular-file check and a size cap;
  - the rebuild's swap is one `renameatx_np(RENAME_SWAP)`;
  - the config op takes `config.lock` before it reads;
  - the fetch profile keeps every Unix socket denied and opens only IP;
  - `-s` / `--strategy` takes only git's built-in strategies;
  - the `HEAD` refusal says "with no project process running";
  - positive controls for the nonce aimed elsewhere, the nested project, N1's fsck refusal and the other-URL
    refusals;
  - slice d names the owned bare repos with `--git-dir` (`gitdirs.at`, `project_git.owned_git`);
  - a push is refused when `ls-remote --symref` fails or the remote has no `HEAD`;
  - the registry's remote is named: `paths.origin`, owner-only.

### What changed since r3, and what that means here

- **P2's per-request link allowance (r3, item 4 of "Step 2 gains") is dropped.** It existed only because mitigation
  2's link rule refused P2's checkout of a tracked link. That rule was removed on 11-15 ("B-1: mitigation 2 removed").
  Nothing refuses links now, so P2 checks tracked links out as git does. The rule's companions in the broker go with
  it, since they were part of option 2 (the B-1 options table: "the broker's refusals"): the refusal of tracked paths
  under `.venv` / `node_modules`, and the commit-time refusal of absolute or escaping link targets. Open point 4 asks
  about the second.
- **A folder named `HEAD` can't be removed or renamed by a fenced writer** (step 1's swap rule). P2 is the project's
  fenced profile, so an operation that would make git remove or rename such a folder can't complete under P2.
  Slice b refuses it before anything moves (below). Open point 6.
- **uv's marker exception is regular-file-only.** P2 inherits it unchanged. P1 doesn't carry the fence at all.
- **P1 is a new profile built from `seatbelt._isolation`**, so it carries every deny there: `mount_denies()`, the raw
  disk device deny (`DISK_DEVICES`), `(deny lsopen)`, the Keychain denies, the process and signal denies. Network off is
  added by P1 itself, as `seatbelt.profile()` does, since `_isolation` has no network rule. It never reuses the fence,
  and it is never a profile with git-dir write and no read rules (r3 S-b).
- **The owner's decisions (11-15, questions 2–6 of r3)** land as follows:
  - push policy: slice d;
  - no signing through the broker: slice a's allowlist and slice d;
  - Codex app worktrees not carved: slice a (a request from outside the project's folders is refused, and the
    message points to a worktree inside the project);
  - no submodules: slice a's allowlist;
  - the owner switches each project on: slice c's rebuild command, which refuses until every writer of the project is
    fenced, so in step 2 it refuses every real project.

### Global constraints (every slice)

- **Throwaway projects only.** Tests make a stand-in project in the throwaway instance `bin/testguard.py` gives a run.
  No test reads or writes a live project, the live `.state` or the instance.
- **The user's global git config is never touched.** Every "owner's git" in a probe runs with `GIT_CONFIG_GLOBAL` set
  to a temp file (holding `safe.bareRepository=explicit` where the precondition is meant to hold, nothing where its
  absence is the control) and `GIT_CONFIG_NOSYSTEM=1`. The broker's own git always has its platform-written global
  file.
- **Linux.** Landlock lacks `git-dirs-fenced`, so the broker refuses there cleanly ("not supported on this OS", exit 2,
  nothing run), through `isolation.unmet`. Every test that needs real Seatbelt skips with that reason elsewhere. The
  pure tests (allowlist parsing, profile text) run on both. `bin/linux-test` and the ubuntu CI stay green.
- **Git in a project's repo runs as the project.** Platform code never runs `git -C <workspace>` or
  `GIT_DIR=<workspace>/.git` as the owner. The broker's own steps as the owner touch only these (r1 S2):
  - repos the platform owns: its bare repo per project, its staging and build folders, named with `--git-dir`
    (`gitdirs.at`, `project_git.owned_git`), never entered with `-C`;
  - a project's git dir, through its no-follow descriptor (the one the record checks), with content the broker
    generates or copies byte for byte:
    - `config` (the config op) and `index.lock`;
    - `MERGE_HEAD`, `MERGE_MSG`, `SQUASH_MSG`, and the `rebase-merge/` files, written and removed;
    - a worktree's admin files (`HEAD`, `commondir`, `gitdir`), written and removed;
    - the rebuild's swap;
  - a project's folders, through no-follow descriptors from the workspace, only to make a new worktree's or repo's
    folder and to write gitfiles;
  - P2's working copy of the index, in the project's platform folder (r1 K1).

  Slice d follows this, which departs from the text above (open point 1).
- **No runtime syntax** in the broker, the wrapper or their tests. Claude's and Codex's renderings are step 4.
  `tests/test_runtime_boundary.py` and `tests/test_tool_agnostic.py` stay green.
- **Merge criterion, every slice: the live check.** With no project switched on, every live project's Seatbelt
  service and cap-run profiles, its Claude settings and env, `render-project-settings --report`'s output and
  `sandboxed-run`'s environment render byte-identical before and after (step 1's live check, repeated). What could
  break it, and how each is held:
  - the socket rule in `service_profile`: rendered only when `policy.git_platform(doc)`;
  - the wrapper first on `sandboxed-run`'s PATH: only when `policy.git_platform(doc)`;
  - `_isolation`'s new `temp` keyword: default `True`, the output unchanged;
  - `git` in `policy.SESSION_SUBCOMMANDS` (it changes `platform_cli()`, and so every sandboxed project's Claude
    settings): not in step 2 (step 4);
  - a new folder in `policy.cli_dirs()` or `deny_write`: none (the wrapper lives under the hub root, already denied);
  - `PRE_COMMIT_HOME` in `policy.cache_env` (r2 S7): not in step 2 (step 3);
  - `isolation.KEEP_ENV`: unchanged (the wrapper's environment rules are its own).
- **Flagged to the owner before merge:** any new service (`local.brunna.git-broker`; open point 12) and any change to
  cap-runner (none planned; open point 19).
- Refusal texts go through `i18n`, with English goldens. Code comments and messages name no local path or private name.

### Slice a: the channel and nonce, P1 and P2, the allowlists, the config op, "not switched on"

**Built:**
- **`bin/seatbelt.py`:**
  - `_isolation(..., temp=True)`: with `temp=False`, the temp area leaves the write allow. The devices stay. Existing
    callers pass nothing.
  - `exec_guard(git_paths)`: `(deny process-exec)`, then an allow for the real git, the Command Line Tools' git and
    git's exec-path, resolved when the broker starts (the prototype's set). Step 3 adds the hook forwarder.
  - `git_writer_profile(pol, git_dirs, readable)` (**P1**): the network denies of `profile()`, then
    `_isolation({**pol, "git_fence": None}, git_dirs, (), readable, temp=False)`, then P1's denied names and pins, then
    the exec guard.
    - `readable` is the project's workspace, its runtime folder and `pol["read"]`, so P1's reads are exactly the
      project's (S-b).
    - Denied names: `config*`, `commondir`, `gitdir`, `hooks/`, `info/attributes`, `modules/`, `remotes/`,
      `branches/`, `objects/info/alternates`, `objects/info/http-alternates`, each
      `worktrees/<n>/{commondir,gitdir,config.worktree}` (plus `shallow` and `info/grafts`, open point 22).
    - Pinned as entries: `objects`, `objects/info`, `refs`, `logs`, `worktrees`, `info`, the platform folder.
    - A call that reads a bundle gets that one file (in `hubconf.private_tmp()`) added to `readable`, for that call
      only (r1 S3). So P1's reads are the project's, plus at most that file.
  - `git_init_profile(pol, git_dir, readable)` (r1 S1): P1's network denies, `_isolation` the same way with the fresh
    `<real path>/.git` as its only writable folder, and the exec guard, but none of P1's denied names or pins: `git
    init` must write `config`, `objects`, `refs` and `info`. The config op then replaces the config whole.
  - `materialiser_profile(pol, dirs)` (**P2**): `service_profile(pol, dirs)` unchanged (it carries the fence), plus the
    exec guard (open point 3).
  - `fetch_profile(pol, staging_dir, readable)` (slice d): P1's rules with only the staging repo writable, and the IP
    network open. Every Unix socket stays denied, as in `profile()` (r1 nit).
  - In `service_profile`, for a switched-on project only: the socket rule in S5's measured form, with
    `(deny network-outbound (remote unix-socket (subpath <state>/git-broker)))`, the whole broker folder and every
    project's sockets in it (r1 S5), and an allow for the project's own **service** socket only.
- **`bin/policy.py`:** `git_broker(doc, name)`, `None` unless `git_platform(doc)`. Otherwise it gives the two socket
  paths, the nonce folder (`<workspace>/.hub-run/git/`) and the wrapper's folder. `policy()` carries it as
  `git_broker`.
- **`bin/sandboxed-run`:** for a switched-on project, the wrapper's folder first on PATH.
- **The wrapper:**
  - `bin/git-wrapper/git`, a POSIX sh front, and the logic in `bin/git_wrapper.py`. The folder holds only `git`. It is
    under the hub root, which every sandbox's `deny_write` already holds (open point 14).
  - Outside a sandbox (`isolation.confined_now()` false), it execs the real git with argv, environment and stdio
    unchanged.
  - Inside a sandbox, it takes one of three paths ("The wrapper"): in place, forwarded, or refused (exit 2, "can't be
    done here (why); nothing was run").
  - When the broker is down, writes say so and fail; reads still run in place.
  - `brunna git <args>`: the same program under `bin/agenthub`, not added to the session subcommands (step 4).
- **`bin/git_allow.py`** (pure, any OS): the allowlists.
  - Every subcommand is in exactly one class: in place, whole under P1, composed, network, config op, or refused by
    name (open point 15 sorts the list in "The allowlists").
  - Per subcommand, a table of exact option spellings and their arity. Short clusters and sticky values are parsed
    against it. `--` and `--end-of-options` end the options.
  - The global options, and the inert `-c` keys.
  - The environment: what is passed on, what makes a call run in place, what is refused.
  - `GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS=true` is always set.
  - Refused by name, with the owner's decisions:
    - `submodule` and every `--recurse-submodules`;
    - `-S`, `--gpg-sign`, `-s` on tags, `--signed`;
    - `init --bare`, `clone --bare` / `--mirror`, `--separate-git-dir`.
  - `-s` / `--strategy` (merge, rebase, cherry-pick, revert, pull) takes only git's built-in strategies (`ort`,
    `recursive`, `resolve`, `octopus`, `ours`, `subtree`): any other name makes git look for a program (r1 nit).
- **`bin/git_broker.py`** (the library; the slices below add to it) and **`bin/git-broker.py`** (the service; started
  only by tests in step 2):
  - **Two sockets per project:** `session` and `service`, in `<state>/git-broker/<project>/` (r3 S-d). A Unix socket
    path is limited to 104 bytes on macOS. The broker checks every path when it binds and fails loudly if one is too
    long (a hashed short name, if instance paths need it).
  - **The nonce** (section 5 and "Identity"):
    - the broker sends it; the caller creates `<workspace>/.hub-run/git/<nonce>`;
    - the broker opens the workspace, `.hub-run` and `git` through `nofollow`'s directory descriptors and checks for a
      regular file; only then does it read the request; the file is removed.
  - **What a request may name:**
    - the working folder and every `-C` must resolve inside the project's folders;
    - the repo's common dir must be one the project owns (`<state>/git-broker/<project>/repos.json`: real path, device
      and inode of its `.git`);
    - **nested or overlapping projects** (r2 S6, r3 S-h; r1 S6 turned the first version's rule the right way round):
      a project isn't served at all when its nonce folder lies inside any other sandboxed project's write roots
      (that project's workspace, runtime folder or `allow_write`), or when the two share a root, since that other
      project could then prove it. Which project a folder belongs to is decided as `test-runner.claimant` does (the
      deepest root wins);
    - a cwd outside the project's folders (a Codex app worktree) is refused, and the message points to a worktree
      inside the project (r3 question 4, decided).
  - **The answer for every project the broker may not serve,** nothing run:
    - "git through the platform isn't on for this project" (`policy.git_platform(doc)` false);
    - "switched on, not rebuilt": no record in `repos.json` for the workspace repo, or its `.git` has another device or
      inode than recorded (open point 7).

    Since no real project is switched on and rebuilt, every real project gets one of these.
  - **`make_repo(project, path)`** (S-a's `init`):
    - the folder is made through no-follow descriptors, and its real path taken from the descriptor;
    - `git init --template=` runs under the init profile, with `<real path>/.git` as its only writable folder (r1 S1);
    - then the config op replaces the config whole (the keys `init` wrote that the allowlist holds, plus the presets),
      and the broker makes the platform folder;
    - then the repo is recorded (real path, device and inode of its `.git`).

    Slice a's tests make their stand-in project's repo with it. Slice b's `init` and slice c's rebuild use it.
  - **The broker's global config** (a file in its state): the owner's `user.name` / `user.email` (read, never written),
    `init.defaultBranch`, `safe.bareRepository=explicit`, `protocol.ext.allow=never`, `protocol.file.allow=user`,
    `core.pager=cat`, `submodule.recurse=false`, `commit.gpgSign=false`, `tag.gpgSign=false`, `gc.auto=0`. It holds no
    drivers, aliases, `credential.helper` or `include*`.
  - **The environment of every git under P1, P2, the init profile and the fetch profile** is built from scratch, never
    inherited from the broker or the request (r1 S4), as `isolation.kept_env` is for a wrapped run:
    - a fixed `PATH`, `HOME` (a platform folder), `LANG`, `TZ`, and `TMPDIR` only where the profile may write it;
    - the request's `GIT_AUTHOR_*`, `GIT_COMMITTER_*`, pathspec variables, `GIT_MERGE_AUTOEDIT`, locale, `NO_COLOR`
      and `TERM`, after the allowlist;
    - the broker's own variables below;
    - nothing else: no `GIT_CONFIG*` beyond the broker's own two, no `SSH_AUTH_SOCK` or other agent sockets.

    The owner's side in slice d gets the broker service's own environment, never anything from a request.
  - **Every git it runs:**
    - `-c core.hooksPath=<an empty platform folder> -c core.fsmonitor=false` on the command line (N3);
    - `transfer.fsckObjects=true` (N1);
    - `GIT_NO_REPLACE_OBJECTS=1`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`;
    - `GIT_EDITOR=:` and `GIT_SEQUENCE_EDITOR=:`.

    The forwarder fills the hooks folder in step 3. Until then, broker commits run no hooks (open point 20).
  - **The operations of this slice** (whole under P1; their worktree is read-only to them): `add`, `rm --cached`,
    `commit` (`-m`, `-F <file>`, `-F -`, `-a`), `hash-object -w`, `apply --cached`, `notes add|append -m`, `gc`.
    Every other forwarded subcommand answers "not built yet" (exit 2, nothing run) until its slice lands.
  - **B2's tree check, here for `commit` and `apply --cached`** (r1 S8; slice b extends it to every other writer of a
    tree or the index): a tracked path whose last component folds to `HEAD` (case, and the characters git's own path
    check ignores) is refused. The broker checks the index `apply --cached` would leave (P1 applies to a copy inside
    the git dir, the broker reads it, and only then is it renamed into place) and the tree `commit` is about to write
    (`write-tree` first, then `commit-tree`).
  - **The config op** (`config <key> <value>`, `--unset`, and the presets):
    - the key and value are checked with `git_fence.check_key` / `url_ok` / `refspec_ok`, narrowed by r2 S3: no
      `remote.*.pushurl`, no `remote.*.push`, no `branch.*.merge` naming a protected ref, and no `submodule.*`;
    - it takes `config.lock` first, created `O_EXCL|O_NOFOLLOW` in the git dir's descriptor (r1 nit). Then it reads
      `config` without following links, edits the copy with `git config --file` in `hubconf.private_tmp()` under
      `GIT_CONFIG_GLOBAL=/dev/null`, writes the result into the lock and renames it over `config`. Never in place.
    - `remote add` / `set-url` / `remove` go through it, with only a URL the registry declares (the fence doc's B4).
  - **Streams, limits, logging:**
    - frames for stdout, stderr, the exit status and a stdin stream;
    - a timeout per call, with the process group killed (open point 17);
    - at most N calls per project at once, queued beyond that;
    - `<state>/git-broker/log.jsonl`: project, subcommand, the path taken, exit code, duration, and no argument
      values.
- **Doctor** (`doctor_git_platform`): a switched-on project's row also says "rebuilt <time>" or "not rebuilt". It
  stays `bad` through all of step 2 (sessions aren't fenced until step 4; hooks and the platform's flows come in steps 3
  and 5).

**Out of scope:**
- the composed operations, `init`, `clone`, `worktree`, ref writes by name (b);
- the rebuild (c);
- everything on the network (d);
- the hook forwarder, the commit gate on it, drivers, the pre-commit framework's repos, lint-staged's token (step 3);
- Claude's `allowUnixSockets`, PATH and fence; Codex's profile and PATH; `brunna git` in the session subcommands; the
  rules text (step 4);
- `project_git`'s write ops, memory sync, `merge-branch`, `with-lock.sh`, `sync-local-setup`, and `session_worktree`'s
  `worktree add` (step 5);
- declaring the service, and the status page's rows (step 6; open point 12);
- the editor callback (step 8). Until then, a caller's own editor is refused with the reason.

**Tests.** Every refused probe has a positive control without the fence or the broker, and the control lands.
- **`tests/test_git_allow.py`** (any OS):
  - each class, and each refusal with its reason: unknown subcommands and options, abbreviations, sticky shorts, `-c`
    keys, environment variables, an alias (refused as an unknown subcommand), the signing and submodule options;
  - a selecting environment goes in place.
- **`tests/test_git_broker_profiles.py`** (profile text, any OS):
  - **P1 carries `_isolation` whole.** Every line of `_isolation(pol', …, temp=False)` except its write allow is in
    P1, so a deny added to `_isolation` later fails this test if P1 is ever built another way.
  - By name: each line of `mount_denies()`, the `DISK_DEVICES` deny, `(deny lsopen)`, the Keychain `mach-lookup`
    deny, and the network denies.
  - No temp area in P1's write allow, and no fence rule in P1.
  - P1's denied names and pins, and the exec guard after everything.
  - P1's builder refuses a policy with no read denies.
  - P2 is `service_profile` plus the exec guard, nothing else.
  - With no project switched on, `_isolation`'s and `service_profile`'s output for a fixture is identical to step 1's
    (a golden). The socket rule appears only for a switched-on fixture.
- **`tests/test_git_broker.py`** (real Seatbelt, real git; skipped without `git-dirs-fenced`):
  - **S-b's four reads under P1, refused:** `hash-object -w <B's file>`, the same through a folder link,
    `commit -F <B's file>`, `apply --cached <B's patch>`. The control is a test-only writer profile with no read
    denies, which puts B's secret into the objects. That profile exists only in the test.
  - **The init profile** (r1 S1): `make_repo` makes a repo whose config holds only allowlisted keys. Under it, a write
    outside the new `.git` is refused (the control: the same write with no profile lands).
  - **B2 in slice a** (r1 S8): a patch adding `x/HEAD`, through `apply --cached`, and a commit of an index holding
    `x/HEAD` are each refused, with the index and HEAD unchanged. The control: plain git takes both.
  - **The environment** (r1 S4): with a canary `http.extraHeader` in the broker's own environment (as
    `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_0` / `GIT_CONFIG_VALUE_0`), a P1 git that contacts slice d's test https
    server never sends it. The control: plain git with the same environment sends it. Until slice d lands, the same
    canary is checked with `git config --get http.extraHeader` under P1, which finds nothing.
  - **P1 can't write** the worktree, the temp area, `config`, `hooks/`, `info/attributes`, the alternates files,
    `commondir` or `gitdir`, or connect to a local listener. The control: the same writes and the connect succeed with
    no profile, and the connect also succeeds under `service_profile`.
  - **The exec guard,** run under P1 with the argument filter turned off: `grep -O<cmd>` and a `!` alias. No canary
    runs. The control: the same commands under P1 without the guard run the canary. (The `rebase --exe` / `-x` probes
    need a worktree checkout, so they are slice b's.)
  - **Identity:**
    - another project's service socket is refused by the socket rule (the control, without the rule, connects);
    - **the project's own service can't reach its own session socket** (r1 S5): directly, through a symbolic link it
      made in its workspace, or through a hard link it tries to make there. The control: without the socket rule,
      each way connects (or the hard link is made and connects);
    - an unrelated Unix socket stays reachable from a service (r2 S5);
    - a forged header with no proof is refused;
    - a nonce aimed at another project's folder is refused (the control: the same nonce in the right project's folder
      is accepted);
    - a FIFO or a link at `.hub-run` or `git` is refused without blocking;
    - **nested and overlapping projects aren't served** (r1 S6): one whose workspace lies inside another's, one
      whose workspace another project's `allow_write` holds, and two that share a root. The control: the same
      project with no overlap is served;
    - a cwd outside the project's folders is refused with the message.
  - **The answers:** not switched on, and switched on but not rebuilt (no record; a `.git` replaced since). Nothing
    runs for either.
  - **The wrapper:**
    - outside a sandbox: argv, environment and exit status passed unchanged;
    - inside: the in-place set runs in place;
    - a selecting environment runs in place, and its write fails loudly with the sandbox's refusal;
    - with the broker down, writes say so and reads work.
  - **The config op:**
    - the allowlist refuses, with value rules and S3's narrowing;
    - the presets are written;
    - the op fails cleanly, before reading anything, when a `config.lock` exists;
    - a FIFO include doesn't hang it.
  - **This slice's operations end to end** through the wrapper, from inside a fenced `service_profile`: `add`,
    `commit -m`, `commit -F -`, `commit -a`, `hash-object -w --stdin` with binary data, `apply --cached`.
  - **The owner's canaries** (section 6) afterwards: none run. The control: the same plants written with no fence run.
  - **Limits:**
    - a timeout kills the group;
    - the concurrency cap queues;
    - an owner's git holding `index.lock` makes the broker wait or fail as a second terminal would;
    - the log holds no argument values.
  - **Goldens** for the refusal texts.

**Done when:** all of the above pass on macOS; Linux skips with the reason and `bin/linux-test` is green; the
boundary and personal-data ratchets pass; the live check is byte-identical; no service is declared.

### Slice b: the composed operations

**Built** (`bin/git_compose.py`, called by the broker):
- **The protocol** (r2 B1, S-c, S-e):
  1. The broker takes `index.lock` exclusively, through the git dir's no-follow descriptor (the one the record checks),
     and holds it to the end.
  2. P1 computes the next index into it.
  3. **The broker writes P2's working copy** (r1 K1): the lock's bytes, read through the git dir's descriptor, written
     as a fresh file (`O_EXCL|O_NOFOLLOW`) into `<workspace>/.git/<platform folder>/git-broker/<op id>/`, the folders
     walked from the workspace through no-follow descriptors (`nofollow`). No other project's sandbox can write
     there: the workspace is this project's alone (r1 S6 refuses the overlapping case), and the fence pins the
     platform folder. A link planted on the way fails the operation; only this project can plant one, and it can
     only stop its own operation. P2 never copies anything itself: under the exec guard it runs only git.
  4. P2 materialises from that copy (`GIT_INDEX_FILE`): `read-tree -m -u`, `read-tree -u --reset`, or
     `checkout-index`.
  5. **P2 checks its own work** on the paths this operation changes (`diff-tree` old → new, plus the conflict paths),
     against the dirty set taken before the operation.
  6. Only then does the broker rename the lock over `index`. P1 moves the refs (`update-ref --stdin`) and refreshes
     the stat information. The working folder is removed.

  If P2 fails or its check fails, P2 puts back what it wrote (`read-tree -m -u <new> <old>` for those paths only, on
  the same working copy). The lock is dropped and no ref moves. Worktree-writing operations run one at a time per
  worktree.
- **Before step 1, the broker refuses an operation that can't complete under P2**, with the reason and nothing moved:
  - **control files** (r1 S11): a tracked path the operation would write or remove that no sandbox of the project may
    write: the runtime control files and folders (`policy.protect`, `policy.protect_roots`, through `runtime_ports`);
  - **a folder named `HEAD`** (below).
- **Git state that no plumbing writes** (r1 S2) is written and removed by the broker, through the git dir's no-follow
  descriptor, with content it generates in git's own format: `MERGE_HEAD`, `MERGE_MSG`, `SQUASH_MSG`, `ORIG_HEAD`
  where git would set it, the `rebase-merge/` files (`git-rebase-todo`, `done`, `head-name`, `onto`, `orig-head`,
  `msgnum`, `end`, `message`), and a linked worktree's admin files (`HEAD`, `commondir`, `gitdir`; P1 is denied the
  last two). `--abort`, `--continue` and the end of an operation remove them the same way.
- **Bundles** (r1 S3): every bundle (a path source read under the project's own profile, a fetched one) goes out on
  stdout into `hubconf.private_tmp()`, and the P1 that fetches it gets that one file as readable for that call. Never
  the shared temp area.
- **The B1 table and S-f's compositions:**
  - `switch` / `checkout <branch>`, including a remote branch under `branch.autoSetupMerge=false`. A requested
    `--track` goes through the config op. `switch -c` with local changes.
  - `reset --hard` / `--merge` / `--keep`; `--soft` and `--mixed` run whole under P1.
  - `restore`, including `--source`, and `checkout <rev> -- <path>`.
  - `merge`: clean, conflicted (the stage entries through `update-index --index-info` under P1, with `MERGE_HEAD` /
    `MERGE_MSG` written by the broker), `--squash`, `--abort`, `--continue`.
  - `rebase`:
    - without conflicts through `replay`;
    - with conflicts, stepwise with `merge-tree`, with `rebase-merge/` written by the broker in git's own format and no
      `exec` lines, so the owner's git can continue it;
    - `--continue`, `--skip`, `--abort`.
  - `cherry-pick` and `revert`, the same shape.
  - `stash push` / `pop` / `apply` / `drop`, including a pop with conflicts.
  - `apply` (worktree only); `am` 2-way (a 3-way `am` is refused).
  - `rm` and `mv` in the worktree: P2 runs `git rm` / `git mv` on its temp copy of the index (so a dirty file moves
    with its changes, and only git runs under the guard); P1 makes the same change to the next index with
    `update-index --index-info`; the check compares the two.
  - `pull` and `clone` from a path: the source is read into a bundle under the project's own profile, and P1 fetches
    the bundle (r2). From a URL, these are slice d's.
  - **`init <dir>`** through `make_repo`.
  - **Scratch repos** (r1 S7):
    - made and served only in folders this project alone can write: its workspace and runtime folder (the test
      runner's per-run `TMPDIR` for a sandboxed project is in its run folder in the workspace, `test-runner.py`), and
      its `allow_write` folders that no other project's write roots hold;
    - never in the shared temp area: any project may write there, so a recorded repo could be swapped between the
      broker's check and P1's run, and P1 would then write another repo as this project. A `git init` there is refused,
      with the reason and the way (`TMPDIR` in the run folder);
    - recorded by real path plus the device and inode of their `.git`; a moved or replaced one isn't served;
    - dropped with their folder.
  - **`worktree add <path> [-b <branch>]`:**
    - the path resolves inside the project's folders, outside every git dir, and doesn't exist yet;
    - the broker makes the folder and writes its gitfile through no-follow descriptors;
    - the broker writes the admin dir's `HEAD`, `commondir` and `gitdir` (r1 S2); P1 writes its index and refs, and P2
      does the checkout.

    `worktree remove`: the worktree's files are removed as the project, under its own profile (as
    `session_worktree.drop`'s removal job does today); only when nothing is left does the broker remove the admin
    files through the descriptor. `prune`: the broker removes admin dirs whose gitfile it can't read back.
- **Under P1 whole, now by name:**
  - `branch`: create and delete; `-m` / `-M` with the config op moving the section; `-u` through the config op;
  - `tag`, with `-a` only with `-m`;
  - `update-ref` and `symbolic-ref` under S1: only `refs/heads/`, `refs/tags/` and `refs/notes/`, and `HEAD` →
    `refs/heads/…` for `symbolic-ref`. Root refs, pseudo-refs and `refs/replace/` are refused;
  - `stash create` / `store`;
  - `update-index` (no option that names a file to write), `read-tree` without `-u`, and `write-tree`, each with B2's
    check on its input;
  - `fetch` from a bundle, with the broker's own refspecs.
- **B2's check:** a tracked path whose last component folds to `HEAD` (case, and the characters git's own path check
  ignores) is refused wherever a tree is about to be written. That covers `write-tree`, `commit`, `merge-tree`'s
  result, `update-index` input, and a fetched branch about to be checked out.
- **The `HEAD` folder check** (step 1's swap rule):
  - before step 1 of the protocol, the broker compares the old and new trees;
  - an operation that would leave no tracked entry under a folder whose name folds to `HEAD` (git would then try to
    remove the folder), or would rename one (`mv`, `rm -r`), is refused before anything moves;
  - the message: a sandbox here can't remove or rename a folder named `HEAD`; do this one outside the sandbox, with no
    process of the project running (r1 nit: while one runs, an unsandboxed checkout can be raced, B-1). Open point 6
    offers the owner a way around it.
- **The check after a stop:** after a broker-run rebase, cherry-pick or revert stops, the broker reads
  `rebase-merge/git-rebase-todo` and `rebase-merge/done` (N2: the sequencer refuses `exec` itself). An `exec` or `x`
  line aborts the operation by composition: P1 puts back the refs and the index, the broker removes the state folder,
  and P2 restores the worktree.

**Out of scope:**
- fetches, pulls and clones from URLs, and pushes (d);
- the rebuild (c);
- moving `session_worktree` and Claude's `WorktreeCreate` path onto the broker's `worktree add` (step 5, open point
  18);
- `add -p` / `-i`, `commit -p`, `checkout -p`: refused, with the `git apply --cached` way said;
- interactive rebase and the editor callback (step 8);
- drivers (step 3).

**Tests** (real Seatbelt; each with its control):
- **Every composition works:**
  - clone from a path; checkout both ways; `commit -a`;
  - merges: clean, conflicted then resolved with two parents, `--squash`, `--abort`;
  - rebase with and without conflicts, through `--continue`, `--skip` and `--abort`;
  - cherry-pick and revert; stash with and without conflicts;
  - `reset --hard`, restore, `checkout <rev> -- <path>`, `switch -c` with local changes;
  - `pull` from a path; `worktree add` / `remove`; `am`.

  Timings are recorded beside r2's table.
- **The race (probe B/C), for every worktree-writing operation.** A project process swaps a tracked folder `d` with
  `ln -s .git d` in a loop while the operation runs. The test asserts that no file of the tree is in `.git`, that
  there is no `rebase-merge/`, and that HEAD never moves on a partial result. The control is the same operation under a
  test-only combined profile (P1's rules plus write to the worktree), which lands files in `.git`. Rounds: open point
  21.
- **P2's working copy** (r1 K1): while a checkout runs, a second sandboxed project tries to replace or swap A's
  working copy of the index. It can't write there, and A's worktree ends exactly as the operation says. The control:
  the same attack against a copy in the shared temp area (the first version's layout) steers a write or a delete in
  A's worktree.
- **Bundles** (r1 S3): another project can't replace a bundle between its making and P1's fetch (it can't write
  `hubconf.private_tmp()`). The control: the same swap of a bundle in the shared temp area lands.
- **Git state written by the broker** (r1 S2): a conflicted merge leaves `MERGE_HEAD` and `MERGE_MSG` that the owner's
  `git commit` (outside, temp global config) completes; a stopped rebase leaves a `rebase-merge/` the owner's
  `rebase --continue` and `--abort` accept; `worktree add` then `remove` leaves no admin dir.
- **Scratch repos** (r1 S7): `git init` in the shared temp area is refused; in the run folder's `TMPDIR` it works. The
  control: a recorded repo in the shared temp area swapped by another project between check and run is entered.
- **Control files** (r1 S11): a checkout of a branch holding a tracked `.mcp.json`, or a file under a runtime's
  control folder, is refused before anything moves, with the reason. The control: plain git under P2 writes part of
  the tree and fails on that file.
- **S-c:** an unrelated local change survives an operation. A failed check puts the worktree back as it was. A retry
  then works.
- **B2:**
  - a commit holding `x/HEAD` is refused (the control: plain git commits it);
  - a folder holding a `HEAD` file, renamed into the workspace and planted with a config: the owner's `status` is
    refused with the precondition in the temp global config, and runs the fsmonitor without it (the control).
- **S1:**
  - `update-ref SHALLOW`, `refs/replace/…`, `MERGE_HEAD` and a root ref are refused;
  - the controls: `SHALLOW` makes the owner's repo look shallow, and a replace ref changes the owner's `show` and
    `checkout -f`;
  - a bundle fetch can't write `refs/replace/`.
- **The `HEAD` folder:**
  - an operation that empties `x/HEAD/`, and a `mv` of it, are each refused with nothing moved;
  - P2's raw behaviour, measured and written down here: what git does when its rmdir is refused (unmeasured so far),
    and the control without the fence removing the folder.
- **An `exec` line in a stopped rebase:** aborted by the check after a stop, and the owner's `rebase --continue`
  afterwards runs nothing. The control: without the check, the canary runs.
- **The exec guard in a rebase,** with the argument filter turned off: `rebase --exe '<cmd>'` and `rebase -x'<cmd>'`
  stop without running the command (and the check after a stop then aborts). The control: without the guard the
  canary runs.
- **S-a's `init` race:** the target is flipped between absent and a link to a fake home. Nothing lands in the fake
  home. The control: the owner's `git init` lands there.
- **The owner's canaries** after every scenario.

**Done when:** all of the above pass on macOS and skip on Linux; one 40-round race run (r2's attack, 90 s of
swapping) per worktree-writing operation has passed and is recorded here, with its date and git version (r1 S12; how
many rounds the `full` tier runs is open point 21); the live check is byte-identical.

### Slice c: the rebuild and its restore

**Built** (`bin/git_rebuild.py`; `brunna git-broker rebuild <project> [--check] [--accept <path>…]` and
`brunna git-broker restore <project> --at <time>`):
- **The shape** (r1 S9):
  - `preconditions(project)` only reads: it opens what it looks at read-only and returns the list of refusals;
  - `rebuild_repo(project, repo, accepted)` does the work for one repo. It is what the tests call, in process, on a
    throwaway project. It is never reachable from the command line without `preconditions` passing: no flag, no
    environment variable and no setting skips them;
  - the command runs `preconditions`, and only with none refused calls `rebuild_repo` for each repo;
  - `--check` runs `preconditions` and prints them, and imports nothing that writes (the write path lives in
    another module, which `--check` never loads).
- **It refuses** unless all of these hold:
  - the registry has the switch on;
  - no session is open (`hubconf.cwd_users`);
  - no rebase, merge, cherry-pick or bisect is in progress;
  - the adapter has `git-dirs-fenced`;
  - every linked worktree's path is in the registry or the platform's records (`.agent-memory`, the tool worktree
    folders under the workspace, walked without following links). Any other is listed (open point 9, for the owner);
  - every `HEAD` entry outside a `.git`, in every area the project may write, and every tracked `HEAD` path on its
    branches, is listed and gone, or accepted by path (r2 B2);
  - doctor's row for the project is ok apart from "not rebuilt". In step 2 that is never true for a real project,
    since its sessions aren't fenced until step 4 (open point 7).
- **The order** (S2): stop the project's services and jobs; rebuild each repo the fence names (the workspace's and the
  runtime folder's; open point 11); record; start them again.
- **Per repo:**
  1. The old git dir becomes a bundle of every ref, made under the project's own profile with the exec guard and
     written on stdout into `hubconf.private_tmp()` (r1 S3). A planted alternates file stops the rebuild and is
     reported; it is never followed.
  2. A fresh dir is made in `<state>/git-broker/<project>/build-<time>/` through `make_repo` (the init profile). P1
     fetches the bundle with `transfer.fsckObjects`, the bundle readable for that call only.
  3. The config: the old one is opened `O_RDONLY|O_NONBLOCK|O_NOFOLLOW`, checked to be a regular file (`S_ISREG`) and
     read up to a size cap (r1 nit); its allowlisted keys go in through the config op.
  4. The index: the old `ls-files --stage`, with its skip-worktree and intent-to-add bits, read under the project's
     profile, then written by `update-index --index-info` and the flags under P1.
  5. Each stash entry through `stash store`.
  6. `info/exclude` and the platform folder's files copied the same way as the config: no links followed, regular
     files only, a size cap. The platform folder itself is made here.
  7. **Each recorded linked worktree** (r1 S2):
     - the broker writes its admin dir's `HEAD`, `commondir` (`../..`) and `gitdir` through the new dir's
       descriptor, and its gitfile through no-follow descriptors from the workspace;
     - P1 writes its own index, with its skip-worktree bits (mitigation 3's left-out files);
     - then the broker reads the gitfile and the admin dir's `gitdir` back through the same descriptors and checks
       that each names the other exactly. `worktree repair` isn't run (open point 8).
  8. **The swap** (r1 nit): one `renameatx_np(RENAME_SWAP)` between the new dir and the workspace's `.git`, on
     no-follow descriptors of both parents, so `.git` is never missing. The old dir, now at the build path, moves to
     `<state>/git-broker/<project>/before-<time>/`. The same volume is checked first, and a different one refuses
     cleanly.
  9. The record (`repos.json`), then `git_fence.py --report --project <p>`.
- Reflogs aren't carried. A shallow repo is refused, with the reason.
- **`restore`:**
  - it puts a kept git dir back by the same swap, and drops the record;
  - then it runs the scan;
  - it refuses while the switch is on: the owner switches the project off first (open point 7).

**Out of scope:**
- the hooks the platform keeps for the owner's own git (`.git/hooks`, S4): re-rendered by the hook installer once step
  5 moves it onto the broker. Until then a rebuilt repo has none, which matters only once a real project is rebuilt;
- the runtime folder's deploy `git pull` (step 5);
- turning the switch on in the registry (the owner's own edit).

**Tests** (real Seatbelt, throwaway repos, through `rebuild_repo` in process):
- **Survives:** staged changes, an uncommitted file, the stash, the `.agent-memory` worktree, a tool worktree with a
  skip-worktree control file, the outbox under the platform folder.
- **Cut:** hard links and symlinks inside the old git dir, the index included. The control shows they are links in
  the old dir.
- **S2's probes:**
  - an `fsmonitor` in the old config doesn't run during the rebuild (the control: the owner's `ls-files --stage` runs
    it);
  - a planted alternates file into project B stops the rebuild, and B's file isn't copied (the control: the owner's
    fetch copies it);
  - an old admin dir's `gitdir` aimed at a fake home: nothing is written there (the control: the owner's
    `worktree repair` writes a gitfile there and `worktree list` shows it);
  - a worktree's gitfile or admin `gitdir` changed between the write and the read-back fails the rebuild (r1 S2; the
    control: unchanged, it passes).
- **The old config:** a FIFO, a link, or an oversized file at `config` stops the rebuild without blocking.
- **The swap:** `.git` exists at every moment (a reader polling it during the swap never misses it).
- **Refused:** a session open; an operation in progress; a worktree outside the records (listed); a `HEAD` entry (and
  accepted with `--accept`); a different volume.
- **No bypass** (r1 S9): the command's options are exactly `--check` and `--accept`, and with the preconditions
  failing, no option, environment variable or setting reaches `rebuild_repo` (each tried; the control: calling
  `rebuild_repo` in process does the work).
- **`--check` can't write:** it runs to the end under a Seatbelt profile that denies every write, and the write
  module isn't loaded.
- **Restore:** the old dir is back byte for byte, and the scan ran.

**Done when:** all of the above pass on macOS and skip on Linux; `brunna git-broker rebuild --check` refuses for every
live project and writes nothing (it is the only form run against them); the live check is byte-identical.

### Slice d: the network path, under the push policy

**Built** (`bin/git_remote.py`):
- **A broker-owned bare repo per project,** `<state>/git-broker/<project>/remote.git`. The broker makes it with
  `init --bare --template=` and writes its config. It is outside every project folder (r3 S-a's broker-owned bare
  repo, used for every network operation; open point 1). Every git in it is named with `--git-dir` (`gitdirs.at`,
  `project_git.owned_git`), never entered with `-C`: the owner's `safe.bareRepository=explicit` refuses a bare repo
  git finds on its own (r1 nit).
- **The registry's remote** is `paths.origin`, which is under `paths`, an owner-only field (`PLATFORM_OWNED`). It is
  the only URL the owner's side ever contacts.
- **`fetch`, `ls-remote`, `pull`'s fetch and `clone` from the registry's remote:**
  1. The owner's git runs in the owned bare repo, with the owner's credential helper and global config and the broker
     service's own environment (nothing from the request), the URL from `paths.origin`, the broker's own refspecs
     (heads and tags, never `refs/replace/`), `-c core.hooksPath=/dev/null`, `--no-recurse-submodules` and
     `transfer.fsckObjects`.
  2. A bundle of what changed goes out on stdout into `hubconf.private_tmp()` (r1 S3) and is fetched under P1 into
     the project's `refs/remotes/<remote>/*` and tags, the bundle readable for that call only. `FETCH_HEAD` is written
     by the broker (r1 S2).
  3. `pull` then merges or rebases as slice b does.
- **`push`** (the owner's decision, r3 question 2):
  - **Sessions only.** On the service socket a push is refused with the reason.
  - **The default branch** is what `ls-remote --symref <paths.origin> HEAD` says, run in the owned repo just before
    the push, and also the registry's branch when it names one. If that `ls-remote` fails, or the remote has no
    `HEAD`, the push is refused (r1 nit).
  - **The project's own non-default branches** (r1 S10; open point 16): a destination `refs/heads/<name>` on
    `paths.origin` that isn't the default branch. "Own" means the project's own remote, the only one it can push to.
    No branch-name rule is added unless the owner wants one.
  - **Refused:** the default branch, tags, `--tags`, `--follow-tags`, `--delete`, `:<ref>`, `--mirror`, `--prune`,
    `--all`, a `+` refspec, `--force`.
  - **`--force-with-lease`:** on the project's own non-default branches only, with the expected value from the
    project's remote-tracking ref.
  - **Nothing under `.github/**`,** unless the registry allows it (open point 16). The check (r1 S10): the pushed
    commit's `.github/` tree is compared with the default branch's current tip's `.github/` tree (fetched into the
    owned repo just before), not with a merge base. Any difference is refused. So a branch based on an older default
    branch must be brought up to date first, if `.github/` changed there since.
  - **The mechanics:**
    1. P1 makes a bundle of the pushed commits, as the project, on stdout into `hubconf.private_tmp()`.
    2. The bundle is fetched into the owned bare repo with `transfer.fsckObjects`, as `project_git.fetch_bundle` does.
    3. The owner's git runs `push --no-verify <paths.origin> <sha>:refs/heads/<dst>` from the owned repo.
    4. P1 updates the remote-tracking ref. `push -u` also sets the upstream through the config op.
  - **`push_on_merge`:** `registry.validate_enforce` refuses `push_on_merge: true` together with
    `enforce.git: platform`. `push_on_merge` isn't in `PLATFORM_OWNED`, so without this a manifest.update could turn
    it on.
- **Fetch or clone from another URL** (https only):
  1. The fetch profile (slice a's `fetch_profile`: P1's rules, the IP network open, every Unix socket still denied),
     whose only writable folder is a fresh staging bare repo in the broker's state, fetches the URL (open point 2).
  2. A bundle of it goes out on stdout into `hubconf.private_tmp()`, and P1, without network, fetches it.

  `ext::`, ssh, and `file://` or a path outside the project's reach are refused.
- **No signing:** the broker's config sets no signing program, and the options are refused (slice a).

**Out of scope:**
- `merge-branch`'s push: step 5. With `push_on_merge` refused for a switched-on project, it doesn't push.
- The runtime folder's deploy;
- Codex app worktrees (not carved);
- submodules.

**Tests** (real Seatbelt). Two kinds of test remote, neither a hole in the URL rule:
- **For the push policy and the refusals:** a bare repo the throwaway platform owns, named in the throwaway registry
  (a platform-owned path is allowed by `url_ok`, as the memory bare repos are).
- **For credentials and https:** an https server on 127.0.0.1 in front of `git http-backend`, needing a credential.
  Its test CA and a test credential helper that records each call are trusted only in the temp global config of the
  owner's side. For the other-URL fetch, the test harness gives the CA to the fetch profile's git directly, at the
  broker's level, never through the wrapper. If this server proves too heavy to keep in the suite, the credential
  path becomes a manual check, listed for the owner with a real remote.
- **Fetch, pull and clone** through the owned repo. The helper is called only on the owner's step, never from P1 or the
  fetch profile.
- **Every refused push form** is refused with its reason, and nothing reaches the remote. The control: plain git
  pushes the same thing.
- **`.github/**`** (r1 S10): a push whose `.github/` differs from the default branch's tip is refused (the control:
  plain git pushes it), including a branch made before a `.github/` change on the default branch that it doesn't
  carry. One whose `.github/` matches lands. With the registry's allowance, a changed one lands.
- **Service socket:** a push on it is refused.
- **The default branch:** a push to it is refused. With `ls-remote --symref` failing (the test server stopped) or a
  remote with no `HEAD`, every push is refused (the control: with both, a push to another branch lands).
- **`--force-with-lease`:** lands with the right lease, and is refused with a stale one.
- **N1:** a project bundle holding a `.git` tree entry is refused by fsck in the owned repo. The control: the same
  bundle fetched without `transfer.fsckObjects` is accepted.
- **No owner git in the project's repo:** every owner-side git call is recorded through the broker's runner, and the
  test asserts that it names the owned repo with `--git-dir` every time.
- **The owned repo under the precondition:** with `safe.bareRepository=explicit` in the temp global config, every
  owner-side step works (the control: the same step entering the repo with `-C` is refused by git).
- **The environment** (r1 S4): the canary `http.extraHeader` of slice a never reaches the test https server from P1
  or the fetch profile.
- **Other URLs:** `file://` outside the project's reach, ssh and `ext::` are refused, each with the control that plain
  git fetches it (for `ext::`, with `protocol.ext.allow=always`); the fetch profile writes only its staging repo, and
  can't connect to a Unix socket (the control: `service_profile` connects).
- **The registry:** `push_on_merge: true` with `enforce.git: platform` fails validation, and so does a manifest.update
  that sets it.

**Done when:** all of the above pass on macOS and skip on Linux, and the live check is byte-identical.

### Open points

Line numbers are this file's as of this section. **(owner)** marks the ones that need the owner's decision. Every
point has a proposed resolution, and the slices above are written to it, the owner's points included. If the owner
decides otherwise, the slice text that changes:
- point 1: slice d's fetch, push and test of "no owner git in the project's repo";
- point 2: slice d's other-URL fetch;
- point 4: slice b's B2 check (a link-target refusal added there);
- point 6: slice b's `HEAD` folder check (the broker would remove an emptied folder instead of refusing);
- point 7: slice a's "not rebuilt" answer, and slice c's refusals and `restore`;
- point 9: slice c's refusal for a worktree outside the records;
- point 12: slice a's done criterion ("no service is declared");
- point 16: slice d's `.github/**` allowance and check, the default branch, and "own branches";
- point 19: nothing in step 2 (cap-runner would change);
- point 21: slice b's race tests (the recorded 40-round run stays either way).

1. **(owner) Owner network operations in the project's repo.** Lines 366–373 run fetch and push as the owner,
   unsandboxed, with `GIT_DIR` on the project's git dir, and lines 1114–1116 keep that for the workspace's own `.git`.
   The platform's rule is that git in a project's repo runs as the project (`project_git`; projects-and-repos.md
   section 4). **Proposed:** every owner network step runs in a broker-owned bare repo (line 1116's mechanism, for all
   repos), with bundles in and out under P1, as slice d says. It is stricter, and costs a copy of the objects per
   project plus a bundle per push and fetch.
2. **(owner) P1 and the network.** Lines 374–376, 954–960, 986–988 and 1135–1136 fetch other URLs under P1 with the
   project's network. This list keeps P1 off the network. The project's own profile can't stage a fetch instead: the
   fence refuses a `HEAD` file anywhere, so it can't make any repo. **Proposed:** a fetch profile (P1's rules with
   outbound network) whose only writable folder is a fresh staging bare repo in the broker's state, never a project's
   git dir. P1 then fetches its bundle.
3. **P2's exec guard, and where P2 works.** Lines 1070–1071 (and S-f) say P2 runs under the exec guard; line 1633
   says P2 is exactly `service_profile` with the fence. **Proposed:** P2's file rules are exactly that, plus the exec
   guard. The guard isn't load-bearing for P2, which holds no right the project lacks, but under it P2 runs only git,
   so it copies nothing itself (r1 K1):
   - the broker writes P2's working copy of the index (the lock's bytes, read through the git dir's descriptor) as a
     fresh file into `<workspace>/.git/<platform folder>/git-broker/<op id>/`, through no-follow descriptors. Only
     this project can write there; the shared temp area, where any project could swap it, is never used;
   - the B1 table's `rm` / `mv` row (line 792: "removed or renamed as the project"): P2 runs `git rm` / `git mv` on
     that copy, so a dirty file is moved with its changes, and P1 makes the same change to the next index with
     `update-index --index-info`. Slice b's check compares the two.
4. **(owner) The commit-time refusal of absolute or escaping link targets** (line 1072). It was listed among option 2's
   parts (line 1094, "the broker's refusals") but not in "What went" (lines 1431–1437). It doesn't close B-1: the
   race needs no tracked link. **Proposed:** dropped with option 2, as the table says. The refusal at carve-out names
   (lines 1062–1063) is moot, since no carve-out exists.
5. **`_isolation` can't give P1 as written.** It always allows the temp area (P1 must not write it: lines 759–760),
   and it emits the fence whenever the policy carries one (P1 must not reuse it: line 1635). **Proposed:** a `temp`
   keyword, defaulting to today's output, and P1 built from the policy with `git_fence` removed. An appended temp
   deny wouldn't do: it would also deny a git dir that lies in the temp area, which every test repo does.
6. **(owner) A folder named `HEAD` under P2** (lines 1532–1534 against B1's order, lines 769–776). Git's behaviour
   when its rmdir is refused is unmeasured. It may exit 0 with an empty folder left, which S8's and S-c's checks
   wouldn't see. **Proposed:** the old→new tree check refuses such an operation before anything moves (slice b), and
   P2's raw behaviour is measured and recorded. **The trade, for the owner:** a project with a tracked folder named
   `HEAD` (rare) then can't empty or rename it through the broker; it has to be done outside the sandbox with no
   project process running. **Offerable instead:** after P2, the broker removes an emptied folder named `HEAD` itself,
   with `unlinkat(AT_REMOVEDIR)` on the parent's no-follow descriptor. That only removes an empty folder (a file
   swapped in fails with `ENOTDIR`), so it doesn't reopen the swap the rule closes. A rename is still refused.
7. **(owner) Switched on, not rebuilt.** The fence renders from the registry alone (lines 1498 and 1627–1629). Lines
   599–618 and 907–908 assume the rebuild comes with the switch, and nothing ties the two together. **Proposed:**
   - the owner sets `enforce.git: platform` by hand, then runs `brunna git-broker rebuild <project>`;
   - between the two, the broker answers "not rebuilt" and git writes fail loudly;
   - the rebuild refuses while doctor's row for the project shows anything but "not rebuilt";
   - `restore` refuses while the switch is on: switching off stays the owner's own edit, never the command's (line
     909 says restore "implies switching the project off").
8. **The rebuild's repair profile** (line 1128: "may write only that worktree's `.git` and its admin dir") writes both
   a worktree and a git dir, which S-f forbids (line 1162). **Proposed (r1 S2):** no `worktree repair` at all. The
   broker writes the gitfile through no-follow descriptors from the workspace (line 1117) and the admin dir's files
   through the new git dir's descriptor, then reads both back through the same descriptors and checks that each names
   the other exactly. A repair run "only as a check" would prove nothing, since it can't write what it would fix.
9. **(owner) Worktrees the records don't name** (lines 1126–1127 take paths only from the registry and the records,
   and say nothing about the others). After the rebuild, their gitfiles would point at an admin dir that no longer
   exists. That includes the Codex app's and the Codex CLI's managed worktrees and any the owner made by hand.
   **Proposed:** the rebuild refuses and lists them. **The trade, for the owner:** while any such worktree exists,
   the project can't be switched on, which gets in the way of the owner's own use of those tools. **Alternative:**
   `--accept-detached <path>`: the rebuild goes on, and that worktree's gitfile is left pointing nowhere (git there
   says "not a git repository"); its files stay, and the owner re-attaches it later with `git worktree add` at the
   same path or moves the work out.
10. **Linked worktrees' own indexes** (line 608 carries only the main index). Mitigation 3 relies on skip-worktree bits
    in a tool worktree's index (line 1290). **Proposed:** each worktree's index is rebuilt with its flags (slice c).
11. **The runtime folder's repo.** Line 1502 fences it, and lines 603–616 rebuild only "the git dir". **Proposed:** the
    rebuild covers every repo the fence names. The deploy's `git pull` there moves onto the broker in step 5.
12. **(owner) Where the broker service lives, and when.** Line 239 says brunna's registry services "like cap-runner",
    but cap-runner is in `modules/sandbox.yaml`. **Proposed:** the sandbox module, declared at step 6, the first
    switch-on of a throwaway project. In step 2 only tests start it. A new service is the owner's call before merge.
13. **`brunna git` in the sessions' permissions** (line 279). Adding it to `SESSION_SUBCOMMANDS` changes every
    sandboxed project's Claude settings. **Proposed:** in step 4, or rendered only for switched-on projects then.
14. **The wrapper's folder** (lines 276–277: "beside the CLI wrapper, under `policy.cli_dirs`"). A new folder there
    changes `deny_write`, and so every rendering, and puts a `git` beside the owner's own CLI. **Proposed:**
    `bin/git-wrapper/` under the hub root, which every sandbox already can't write and which no PATH of the owner's
    holds.
15. **The allowlist's scope.** Lines 388–392 against line 1163 (S-f: "exactly the composed set"). **Proposed:**
    - `count-objects`, `fsck` (without `--lost-found`), `range-diff`, `format-patch --stdout`, `version`, `help` and
      `hash-object` without `-w` run in place: they only read;
    - `update-index`, `read-tree` without `-u`, and `write-tree` run under P1, with B2's check on their input;
    - `bisect` isn't in either list, so it is refused whole, not only `bisect run`;
    - `notes` without `-m`, and `rebase -i`, are refused until the editor callback (step 8).
16. **(owner) Push policy fields, and what the push checks compare.**
    - `push_on_merge` isn't owner-only (`registry.PLATFORM_OWNED`); slice d refuses it with the switch.
    - The `.github/**` allowance (lines 914–915 and S-d) has no field. **Proposed:** an owner-only key under
      `enforce` (for example `enforce.git_ci: true`); the owner picks the name.
    - **What `.github/**` is compared with** (r1 S10). Line 914 says "the diff between the destination's current
      commit and the pushed one", and the first version of this list used a merge base for a new branch. Both let a
      branch carry an old `.github/` from before a change on the default branch. **Proposed:** the pushed commit's
      `.github/` tree against the default branch's current tip; any difference is refused. The cost: a branch has
      to be brought up to date when `.github/` changed on the default branch.
    - **"The project's own non-default branches"** (r1 S10; lines 1003 and 1202). **Proposed:** any `refs/heads/`
      name on `paths.origin` but the default branch (the remote's `HEAD`, and the registry's branch when it names
      one); a push is refused when the remote's `HEAD` can't be read. No naming rule, unless the owner wants one
      (for example only `<project>/…` or `agent/…`).
17. **The timeout is "a registry setting"** (lines 488–489). Machine-wide limits sit in `brunna.json` (as
    `cap_runner.*` do). **Proposed:** `git_broker.timeout_s`, `git_broker.long_timeout_s` and
    `git_broker.concurrency` in `brunna.json` and `contracts/hub.v1.schema.json`.
18. **Work with no step.** `session_worktree`'s `worktree add` as the project (line 1621) isn't in step 5's list (lines
    690–691). Services' and jobs' PATH (lines 497–498) isn't in step 4's wording (line 688). **Proposed:**
    `session_worktree` in step 5; services' and jobs' PATH and socket rule in step 2 (slice a), since only the platform
    renders them.
19. **(owner) cap-run and git.** `seatbelt.profile()` denies every Unix socket, so a cap-run job can't reach the broker,
    and its git writes in a switched-on project fail under the fence. **Proposed:** cap-runner is unchanged, and those
    failures are loud and expected. Ask if a capability job ever needs to commit.
20. **Hooks in step 2.** Until the forwarder (step 3), broker commits run no hooks, so no commit gate. Only throwaway
    projects are served, and doctor stays `bad` for a switched-on project (lines 1556–1562), so nothing real depends
    on it.
21. **(owner) The race probes' cost.** r2 ran 40 rounds over 90 s per operation (line 817). About fifteen operations
    at that cost don't fit the `full` tier's minute. **Proposed:** a few rounds per operation in `full` (the race
    landed in the first round in r2 and r3), and the 40-round run as an opt-in run. Whatever the owner decides for
    `full`, slice b isn't done until one 40-round run per worktree-writing operation has passed and is recorded here
    (r1 S12).
22. **P1's denied names, two additions:** `shallow` (S1 showed it changes what the owner's git sees) and `info/grafts`
    (git reads it as history, and only a hand writes it). Neither is in the list at lines 330–332.

### The owner's decisions on the open points (11-15)

The owner took every recommendation (the slices are already written to them):
- **Point 1:** network git with the owner's credentials runs in a broker-owned bare repo, bundles in and out; never as the
  owner in the project's `.git`.
- **Point 2:** P1 has no network. Fetches from other https URLs run in the separate fetch profile, which writes only a
  staging repo; P1 fetches its bundle.
- **Point 4:** the commit-time refusal of absolute or escaping link targets is dropped with mitigation 2.
- **Point 6:** an operation that would empty or rename a tracked folder named `HEAD` is refused before anything moves;
  the broker's own removal of an emptied one stays ready to offer if such a folder turns up.
- **Point 7:** two steps: the owner sets `enforce.git: platform`, then runs `brunna git-broker rebuild`; the broker
  answers "not rebuilt" in between; `restore` refuses while the switch is on.
- **Point 9:** the rebuild refuses while the project has worktrees the platform doesn't know, and lists them;
  `--accept-detached <path>` leaves one detached for the owner to re-attach.
- **Point 12:** the broker service goes in the sandbox module beside cap-runner, declared at step 6; in step 2 only
  tests start it.
- **Point 16:** the owner-only key for allowing `.github/**` is `enforce.git_ci: true`; the `.github/` check compares the
  pushed commit with the default branch's current tip; "own non-default branches" is any branch on `paths.origin` but
  the default.
- **Point 19:** cap-run jobs don't commit in a switched-on project; cap-runner is unchanged and git writes there fail
  loudly.
- **Point 21:** the `full` tier runs a few race rounds per operation; the 40-round run is opt-in, and one recorded
  40-round run per worktree-writing operation is part of slice b's done criterion.

### Slice a as built (11-15)

Built as the slice says, with the owner's decisions above. **No project is switched on;** every live project's renderings
were compared before and after (below). The broker is started only by tests; no service is declared.

**What landed:**
- `bin/seatbelt.py`:
  - `_isolation(..., temp=True)`;
  - `exec_guard(git_paths)` (a path ending in `/` is a folder: git's exec-path);
  - `git_writer_profile` (P1), `git_init_profile` and `materialiser_profile` (P2), as the slice lists them;
  - `broker_socket_rules`, in `service_profile` for a switched-on project only.
- `bin/policy.py`: `git_broker(doc, name, state=None)`, carried by `policy()` as `git_broker`.
- `bin/sandboxed-run`: for a switched-on project, `bin/git-wrapper` first on PATH and the service socket named in
  `<prefix>GIT_SOCKET`.
- `bin/git_allow.py`: the allowlists, pure.
- `bin/git_broker.py`: the library. `bin/git-broker.py`: the service's entry point. `bin/git_wrapper.py` with
  `bin/git-wrapper/git`, and `brunna git`.
- Doctor's switched-on row says "rebuilt <time>" or "not rebuilt", and stays `bad`.
- `git_broker.timeout_s`, `long_timeout_s` and `concurrency` in `contracts/hub.v1.schema.json` (open point 17).

**Measured** (`tests/test_git_broker.py`, real Seatbelt, real git 2.50.1). Every refused probe has a positive control,
and each control lands:
- **S-b's four reads** (`hash-object -w` of B's file, the same through a folder link, `commit -F` of it,
  `apply --cached` of B's patch), each through the wrapper and the broker into P1:
  - each one reached P1 (the log says so), failed there, and left nothing of B's in A's repo;
  - the control, a test-only writer profile with no read denies, put B's secret into a fresh repo each time.
- **P1:**
  - it can't write the worktree, the temp area, `config` (`CONFIG.lock` too), `hooks/`, `info/attributes`,
    `info/grafts`, `shallow`, either alternates file, `commondir` or `gitdir`;
  - it can't connect to a TCP or Unix listener;
  - it writes its git dir;
  - controls: each write and connect lands with no profile, and each connect also lands under the service profile.
- **The exec guard:** git run directly under P1 (no argument filter): `grep -O<cmd>` and a `!` alias ran no canary.
  P1 without the guard ran both.
- **The init profile:** `make_repo`'s config holds only allowlisted keys plus the preset, it has no hooks folder, and it
  has the platform folder. A write outside the new `.git` is refused under the profile and lands with none.
- **B2:**
  - `apply --cached` of `x/HEAD`, `x/head` and a spelling with an ignorable character is refused; the index and HEAD
    are unchanged, and no copy or lock is left;
  - a commit of an index holding `x/HEAD` (put there by the owner) is refused, with nothing changed;
  - control: the owner's plain git takes both.
- **The environment:** a canary `http.extraHeader` in the broker's own environment (`GIT_CONFIG_COUNT`/`KEY_0`/
  `VALUE_0`) isn't seen by P1's `git config --get`. Plain git with the same environment sees it.
- **Identity:**
  - another project's service socket is refused under the socket rule; without the rule it connects;
  - the project's own session socket is refused directly, through a symbolic link it made, and through a hard link it
    tried to make (see the deviations for that control);
  - an unrelated Unix socket stays reachable;
  - no proof, and the nonce in another project's folder, are each refused; the same nonce in the right folder is
    accepted;
  - a sandboxed process connected to another project's socket can't write that project's nonce (the wrapper says so);
    that project's own service can;
  - a FIFO or a link at `.hub-run` or `git` is refused at once;
  - a nested workspace, a nonce folder in another project's `allow_write`, and a shared root are each not served; the
    same project with no overlap is served;
  - a cwd or `-C` outside the project's folders is refused, and told to use a worktree inside the project.
- **The answers:** not switched on; switched on but not rebuilt (no record, and a `.git` replaced since). Nothing runs
  for either.
- **The wrapper:**
  - outside a sandbox, argv, environment and exit status pass unchanged;
  - inside, reads run in place (no broker call);
  - a selecting environment runs in place and its write fails loudly;
  - with the broker down, a write says so (exit 2) and a read works.
- **The config op:**
  - refused: `core.fsmonitor`, `core.bare=true`, a filter, `remote.*.pushurl`, another URL, `branch.main.merge`,
    `submodule.*`, `--global`, a remote with an undeclared URL, and unsetting `core.repositoryformatversion`;
  - lands: `user.name`, `remote add` of the registry's URL, an upstream on a non-default branch, `--unset`, and
    `remote remove` (its tracking refs go too);
  - with an existing `config.lock` it exits 255 and leaves the owner's lock and the config as they were;
  - a FIFO include doesn't hang it.
- **This slice's operations end to end,** through the wrapper from inside the fenced service profile: `add`,
  `commit -m`, `commit -F -`, `commit -a`, `hash-object -w --stdin` (300 KB of binary), `apply --cached` (from stdin
  and from a file), `rm --cached`, `notes add -m`, `gc`. The repo fscks clean afterwards. The other forwarded
  subcommands answer "not built yet".
- **The owner's canaries:**
  - the section 6 plants were written from inside, through `git` (the wrapper) and directly: a filter with
    `.gitattributes`, `core.fsmonitor`, a `post-index-change` hook, `info/attributes`, a nested repo with its own
    fsmonitor;
  - the owner's `status`, `add`, `commit`, `diff` and the nested `status` then ran none of them;
  - control: the same plants written unfenced, without the wrapper, ran all four.
- **Limits:**
  - a call over the timeout exits 124 and leaves no process;
  - with the cap at 1, a second call waits until the first ends;
  - the owner's `index.lock` makes `add` and `apply --cached` fail with exit 128 and git's message;
  - the log holds project, socket, subcommand, path, exit and duration, and no argument value.

The profile text (`tests/test_git_broker_profiles.py`, any OS):
- P1 holds every line of `_isolation` (but its write allow), each of `mount_denies()`, the raw-disk deny,
  `(deny lsopen)`, the Keychain deny and the network denies;
- P1 has no temp area and no fence rule, its denied names and pins, and the guard last;
- P1 and the init profile refuse a policy without the read rules;
- P2 is the service profile plus the guard;
- the socket rule renders only for a switched-on project, and sandboxed-run's environment changes only for one;
- with nothing switched on, `_isolation` and `service_profile` match a golden written from the code before this slice,
  at fake paths, so it reads the same on any machine and OS.

The allowlists: `tests/test_git_allow.py`. The refusal texts: `tests/test_i18n_golden_git_broker.py` (zh-Hant and en).

**Live check (11-15, this machine).** For each of the 29 registered projects, compared before and after:
- the Seatbelt service profile (each declared port) and cap-run profile (each capability);
- the Claude settings and env, and the policy's env;
- `sandboxed-run`'s environment (`env` run in each existing sandboxed workspace);
- `render-project-settings --report`'s output and exit code (1 before and after, with the same output: a project's
  drift unrelated to this change).

All were byte-identical. The policy dict gained only the `git_broker` key, `None` for every project.

**Deviations from the text above, each the safer reading:**
- **Socket paths over macOS's 104 bytes** are bound and reached by their name from their folder (`unix_bind`,
  `unix_connect`) instead of being refused.
  - The tests' temp folders (and `brunna test`'s) go past it, and a long instance path could.
  - Measured: the sandbox's socket rule still matches the full path, and a sandboxed process can `chdir` into the
    read-denied state folder to connect. So the rule holds as written.
- **The broker's global config, its empty hooks folder and its `HOME`** are in the shared platform state
  (`hubconf.shared()/git-broker/`), not in `.state`.
  - P1 must read them, and `read_denies` denies `.state` whole.
  - `shared()` is already in `policy.read`, so P1 reads them under the project's own rules, with no widening.
  - What they hold: the owner's `user.name` and `user.email` (which the project's commits carry anyway, and which
    `~/.gitconfig`, readable by every sandbox, holds) and fixed keys.
- **B2 for `commit`** checks the index the commit is written from, read through the git dir's descriptor, instead of a
  `write-tree` and `commit-tree` pair.
  - `commit -a` and a pathspec commit add no path the index lacks.
  - The index is written only by git, through the broker (checked) or by the owner.
  - So the paths are the same, and `commit` keeps git's own semantics (`--amend`, message cleanup, the reflog).
- **The nonce walk** opens `.hub-run` and `git` with `O_NONBLOCK` as well as `O_NOFOLLOW` (its own opener). `nofollow`'s
  doesn't, and a FIFO there must not block.
- **The hard-link control.** Measured: `link()` of the session socket into the workspace is refused under every profile
  of the project, with or without the socket rule or the read deny, because the source is outside its writes. So the
  hard link's control is no profile at all, where it is made and connects. The symbolic link's and the direct
  connect's controls are the service profile without the rule.
- **The repo the broker checked is pinned:** P1 runs with `GIT_DIR` and `GIT_WORK_TREE` set to it, and every `-C` is
  resolved by the broker. Git's own discovery is not used.
- **A process given no socket** (no `<prefix>GIT_SOCKET`) runs git in place. Its writes then fail under the fence, or
  land for a project that isn't switched on, as without the wrapper.
- **The environment the wrapper sends** is `git_allow.relevant_env`: every `GIT_*`, `LC_*`, the passed-on names and the
  editors. Ignored, said in `git_allow`'s header: `GIT_PAGER`, `PAGER`, `GIT_TERMINAL_PROMPT`, `GIT_PROGRESS_DELAY`,
  `GIT_FLUSH`, `GIT_OPTIONAL_LOCKS`, `GIT_ADVICE`, and two the broker sets itself. `GIT_TRACE*` to stderr is passed on.
  Any other `GIT_*` refuses a forwarded call.
- **A caller's editor** is refused only where git would open one: a commit with no message source, `-e`, `-c`,
  `--squash`, `--fixup=amend:`/`reword:`, and notes without `-m`. An `EDITOR` that is set changes nothing else.
- **"Protected" for `branch.*.merge`:** `main`, `master`, the broker's `init.defaultBranch`, and the registry's
  `paths.branch` when it names one.
- **`remote remove`:**
  - the config section is removed by the config op;
  - the tracking refs are deleted under P1 (`for-each-ref`, then `update-ref --stdin`);
  - `branch.*.remote` entries naming it are left.
- **`config --unset`** refuses `extensions.*` and `core.repositoryformatversion`, which say how the repo is stored.
- **Linked worktrees** are served when their pointers agree (`gitdirs.confirmed`) and their common dir is recorded.
  P1 writes the common dir, which holds the admin dir.
- **`repos.json`** keeps `{git, dev, ino, workspace, at}` per repo. `workspace: true` marks the workspace's own repo,
  which doctor reads.
- **Not built here** (their slices): `fetch_profile` (slice d), and the one bundle P1 may read for a call (slices b–d:
  no slice-a operation reads one).
- **`brunna git`** is in the CLI's command table but not in its `--help` text. That text is translated and pinned by
  goldens, and the command is the wrapper's, not a session subcommand until step 4.
- **The nonce folder** `.hub-run/git/` stays in the workspace, empty, after a call. Git doesn't show an empty folder,
  and cap-runner's exclude covers `.hub-run/` for the projects it serves.

**After the slice's blind code review (11-15).** Nothing blocking. Its should-fix items and nits, each fixed and tested
with a control:
- **S1, a timeout left `index.lock`.** `sleep 1e9 | git commit -a -F -`: git takes `index.lock`, then waits for its
  message. The timeout's SIGKILL left the lock, which blocked the owner's git and the project's; the project can't
  remove it under the fence.
  - Now the broker reads a message given on stdin (`-F -`, capped at 1 MiB) whole before git starts, within the call's
    timeout.
  - Every stop is SIGTERM to the group first (git removes its lock files), then SIGKILL after 2 s.
  - Tested: the review's case exits 124 and leaves no lock, and the project's next call works. `_stop` on a git holding
    `index.lock` leaves no lock; the control, SIGKILL alone, leaves it.
- **S2, exhaustion by any sandboxed project.** The socket rule is rendered only for a switched-on project, so any other
  sandboxed project's process can connect to every broker socket. Before, each connection got a thread with nothing
  bounding it.
  - The handshake has one deadline (`handshake_s`, 10 s) for the whole of it, not a timeout per read, so dripping
    bytes gains no time.
  - Connections not yet proved are capped per socket (`unproved_per_socket`, 16) and in all (`unproved_total`, 64).
    Beyond that, a connection is turned away at once with the reason.
  - A project's waiting calls are capped (`queue`, 16); the next is refused with the reason.
  - When the service is out of descriptors, accept backs off. The service raises its open-file limit toward 8192.
  - `log.jsonl` moves to `log.jsonl.1` past 4 MiB.
  - Tested, each with its control: a dripping caller is dropped within the deadline while one in time is served; the
    per-socket cap (another socket of the same project still serves) and the total cap, each served again once the
    idle connections close; the queue bound, free again afterwards; three `EMFILE` accepts backed off and the call
    still served; the open-file limit raised from 256; the log rotated under a small bound and not under the real one.
  - The bounds are its own settings, in `contracts/hub.v1.schema.json`.
- **N1:** a gitlink (mode 160000) is refused in the same index scan as B2's check, for `commit` and for
  `apply --cached`. Plain git takes both (control).
- **N2:** P1 also may not write `rebase-merge/`, `rebase-apply/`, `sequencer/` or `info/sparse-checkout`, in the git
  dir or in a worktree's admin dir. This is defence in depth: no slice-a operation writes them, and slice b's broker
  writes the sequencer state itself. Each is refused under P1 and lands with no profile.
- **N3:** `overlap()` also refuses a project:
  - whose recorded repo lies in another sandboxed project's folders (switched on or not), even when its nonce folder
    doesn't;
  - whose workspace is in the shared temp area.

  Controls: the same project without the overlap, or outside the temp area, is served.
- **N4:** the config op takes `config.lock` before it reads the config. Tested: with the owner's lock present and the
  config replaced by a FIFO, it exits 255 on the lock. Without the lock, the FIFO stops it another way, and it leaves
  no lock.
- **N5:** a remote's name may hold a dot (`remote.a.b.url`): keys are split at their last dot (`remote_of`). `remote add
  a.b`, `set-url a.b` and `remove a.b` work, and a second `add a.b` is "already exists".

**The live check, again (11-15).** A project was registered in between, so the first baseline no longer matched the
live registry. The renderings were compared back to back instead, under the same registry and from the same checkout:
`seatbelt.py` and `policy.py` from before slice a against the branch's own.
- All 30 projects' service and cap-run profiles, Claude settings and env, and policy env were byte-identical.
- The policy gained only `git_broker`, `None` for every project.
- `sandboxed-run`'s environment was identical to the first baseline.
- `render-project-settings --report` still exits 1. Its counts moved only with the new project.

**Step 6, before the service is declared (from the review's S2):** render the broker folder's socket deny
(`(deny network-outbound (remote unix-socket (subpath <state>/git-broker)))`) into every sandboxed project's profiles,
switched on or not, not only switched-on ones. It isn't rendered now because it would change every sandboxed project's
renderings, which slice a keeps byte-identical. Until it lands, the bounds above are what stops a project that isn't
switched on from exhausting the broker.

**Open for the owner (not blocking):** the broker's global config lives in the shared platform state (above). It holds
the owner's name and email, which the projects already see, and nothing private. Say if it should hold the identity
elsewhere: for example, the identity passed with `-c` per call. That changes git's precedence, since the project's own
config could no longer set it.

### Owner-run push, built first (11-15)

**What.** `brunna push <project> [<branch>] [--yes]` (`bin/git_remote.py`), run by the owner in a terminal. It pushes
one branch of a sandboxed project to the registry's `paths.origin` with the owner's git and credentials, and never runs
git in the project's repo. It works today for every sandboxed project, switched on (`enforce.git: platform`) or not.

**Why first (the owner, 11-15).** A sandboxed project can't reach the owner's credentials, so its pushes are already
the owner's, by hand, in the project's repo. That runs the owner's git over a `.git` the project can write: its hooks,
`core.fsmonitor`, `core.sshCommand`, `credential.helper`, filters. That is project-planted code run as the owner
outside every sandbox, today, on every push. The push half of slice d fixes that without slices a and b. A session's
push over the broker socket still needs them and is not part of this.

**How** (each step is the slice d mechanism, run by the owner instead of the broker service):
1. The project must be sandboxed (`isolation.sandboxed`; a project in `service_sandbox_exempt` is refused, since its
   jobs run unsandboxed). The remote is `paths.origin`, an owner-only field, and it is the only URL contacted. Refused:
   none, `(native)`, `ext::` / `fd::`, a local spelling that isn't an absolute path (relative, `file://host/…`), and a
   local path, as named or resolved, in a folder a sandboxed project writes or in a temp area (every sandboxed
   project's processes can write those: `/tmp`, `/var/tmp`, `/private/tmp`, `/private/var/folders`, `/dev/shm`, the
   temp folder of the run).
2. The branch is bundled as the project: `project-git-job.py bundle-branch` through `project_git.job`, sandboxed. Every
   job's environment is built (`project_git.job_env`: `isolation.kept_env`, as `run_as_project`), never the caller's:
   the owner's terminal may hold tokens, and `git status` in the job runs the repo's fsmonitor. The job runs the real
   git by the absolute path `project_git` resolved, since sandboxed-run puts the platform's wrapper first on PATH for
   a switched-on project and the wrapper refuses `bundle` (this holds for every op: bundle-head, bundle-unpushed,
   memory-sync). It
   leaves out what the remote's tips reach, the ones the project has (a bundle's prerequisites must exist on both sides).
   Its stdout lands in a file under `hubconf.private_tmp()`. When the remote already has every commit (a new branch at
   a known commit, say), the job prints only the branch's commit id, and the push goes on only if the owned repo has
   that commit.
3. The owner's git runs only in `<state>/git-broker/<project>/remote.git`. That repo is made with
   `init --bare --template=`, its config is written by the platform, and it is named with `--git-dir` every time
   (`safe.bareRepository=explicit` holds). Its cwd is that folder. Its environment is built from scratch: the owner's
   HOME, PATH, global config, ssh agent and proxies, and nothing else. In order:
   - `ls-remote --symref` gives the default branch;
   - a fetch of the default branch and the pushed branch runs with hooks off, no submodules, no tags and fsck on;
   - the bundle goes into a staging bare repo that borrows the owned repo's objects (no checks there), and from there
     into the owned repo over `file://`, with `transfer.fsckObjects`. A fetch of a bundle checks objects only on newer git; a fetch
     over a transport does on every git. A `.git` tree entry is refused there (`hasDotgit`).
4. **The policy:**
   - fast-forward only, with no force of any kind;
   - no tags, no deletes, no refspecs, one branch per call;
   - **the default branch is allowed here** (slice d refuses it for sessions). The owner runs this and sees every
     commit first;
   - the default branch is the remote's `HEAD` (`ls-remote --symref`). When that can't be told, as with an empty
     remote, the push is refused, so a repo's first push is not this command's;
   - `.github/**`: the pushed commit's `.github` entry (mode and object) is compared with the remote default branch
     tip's. If it differs, the push is refused unless the registry has `enforce.git_ci: true` (owner-only, needs
     `enforce.sandbox`). With the allowance it is still listed first. The base differs from slice d's (the
     destination's current commit, or the merge base for a new branch) because GitHub runs the workflows of the pushed
     commit's own tree, and the default branch's are the ones the owner has already accepted.
     **What the gate doesn't stop:** workflows that already exist run on the pushed code, and the code they run
     (scripts, test suites, build files) has no gate; nor do other forges' CI files (`.gitlab-ci.yml`,
     `.circleci/`, `Jenkinsfile` and the like). It keeps a push from changing which workflows run with the repo's
     secrets, not what those workflows execute.
5. **What the owner sees:**
   - the range;
   - each commit's short sha, author name and email, and subject;
   - the changed paths;
   - `.github/` flagged;
   - author emails absent from the remote default branch's last 500 commits, flagged (the registry has no field for a
     project's usual or anonymous email).

   Control characters in anything the project wrote are shown as escapes. The command then asks y/N. `--yes` skips
   that; with no terminal and no `--yes` it refuses.
6. `push --no-verify --no-follow-tags --recurse-submodules=no --force-with-lease=refs/heads/<branch>:<old> --
   <origin> <sha>:refs/heads/<branch>` runs from the owned repo, pushing the exact sha shown. The lease pins the push to
   the remote's ref as shown (empty for a new branch): if it moved while the owner read, even backwards, the push is
   refused. It forces nothing, since `<old>..<sha>` was checked as a fast-forward. The project's repo is never written, and its `origin/<branch>` moves on its
   own next fetch. Slice d's step 4 (P1 updates the remote-tracking ref) waits for the broker.
7. No bus event type fits a push, and `merge.done` would trigger event jobs. Each push is a line in
   `<state>/git-broker/<project>/pushes.jsonl`.

**What differs from slice d's session push:**
- the owner starts it, so there is no socket, no nonce, no P1, and no service;
- the default branch is allowed;
- `--force-with-lease` only pins what was shown; nothing the owner names is forced;
- the remote-tracking ref isn't updated;
- the first push of an empty remote isn't covered.

When slice d lands, sessions push their own non-default branches through the broker, and this command stays the
owner's way to push the default branch.

**Tests** (`tests/test_git_remote.py`):
- the remote is a bare repo in the test's temp folder, plus a `git http-backend` behind basic auth on 127.0.0.1 with a
  recording credential helper in the owner's temp global config. Plain http: TLS is git's own, checked by hand
  against a real remote;
- every refusal is tested against a plain-git control that pushes the same thing;
- every owner-side git call is recorded at its one start point, and each one names an owned repo with `--git-dir`,
  from outside the workspace;
- a canary planted in the project's `.git` never runs, with the control that the owner's git in the workspace runs it;
- a token in the owner's environment never reaches the job's fsmonitor through the real `sandboxed-run`, with the
  control that the caller's environment passed through hands it over;
- a remote rewound while the owner reads refuses the push, with the control that a push without the lease lands the
  commit never shown;
- a switched-on project's job bundles through the real `sandboxed-run` (macOS), with the control that the wrapper
  refuses `bundle`;
- local origins: relative, `file://host/…`, temp areas, a project's folder through a link or `file://`, each refused,
  with plain-git controls;
- on macOS, the project's job runs under the real Seatbelt profile, with and without the git-dir fence;
- one end-to-end run types `brunna push` in a throwaway instance: the registry entry read through its index and the
  job through the real `sandboxed-run` (Seatbelt on macOS, Landlock in `bin/linux-test`).

### Platform push supersedes slice d's session push (11-15)

The owner's principle (11-15): no person runs `git push`; a project's agent says "push" and the platform does it.
`platform-push.md` designs that for sandboxed projects. Nothing above is deleted; where they differ, that doc holds for
pushing:
- **Superseded:** slice d's push over the session socket ("sessions only", the project's own non-default branches,
  `--force-with-lease` there, P1 updating the remote-tracking ref after a push). A push is a request in the project's
  outbox, taken in by `hubbus dispatch`, and run by a detached worker through `git_remote.py`'s pipeline (this
  section's "Owner-run push") with automatic checks in place of the owner's look.
- **Revisited:** r3 question 2's "sessions push only non-default branches" becomes the owner's per-project field
  `enforce.git_push.branches: none | others | all`. The rest of that answer stands (no tags, `.github/**` only with
  `enforce.git_ci`, no signing). Slice d's `validate_enforce` refusal of `push_on_merge` with `enforce.git: platform`
  is dropped: in a sandboxed project `push_on_merge` only files a request, which the owner's field decides.
- **What the broker still gives:** the fence (the owner's unsandboxed git, editors and agent tools safe in a project's
  repo), commits and config through the platform, brunna's own git flows (step 5), and sessions' network reads
  (fetch, pull, clone, other URLs). A session socket later makes "sessions only" enforceable, as an option.

**Order of work, revised (11-15):**
1. Platform push (`platform-push.md` section 9). It needs nothing from slices b–d.
2. Slices b and c, then slice d without its push half.
3. Steps 3–8 as before; step 5's `merge-branch` push is a platform push request.
