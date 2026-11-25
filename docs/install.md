# Installing brunna

The one line from the [README](../README.md):

```bash
curl -fsSL https://raw.githubusercontent.com/imaknas/brunna/main/install.sh | sh
```

## What the installer does

The installer clones the repo into `brunna` in your home folder (`BRUNNA_DIR` to choose another), checks out the
newest release, installs [uv](https://docs.astral.sh/uv/) if it is missing, runs `bin/setup`, and goes straight on
into `brunna init`, whose first question is the language. git is the one thing it may install, and it asks first
(on macOS, Apple's command line tools; on Linux, your package manager with your own `sudo` password). `bin/setup`
names anything else that is missing with the command to install it. Pasting the line again after an install that
stopped halfway carries on from where it stopped. Without a terminal (CI, a container) it stops after the install;
`curl … | sh -s -- --no-init` does the same on purpose.

After that, the README's [Getting started](../README.md#getting-started) takes over.

## By hand

Read the installer first if you like: [`install.sh`](../install.sh). The same by hand:

```bash
git clone <this repo URL> brunna && cd brunna   # any folder; hooks name it where it is
git checkout "$(git describe --tags --abbrev=0 --match 'v[0-9]*')"   # the newest release (stay on main to follow development)
bin/setup                            # check required tools, build the platform Python, put `brunna` on PATH
brunna init                        # the questions (open a new terminal first if brunna isn't found)
```

With a model set up (`brunna llm setup`) and the scheduler running, the next morning there is a digest and a `todo.md`
in the instance's data directory. Without a model they are not written: the nightly tidy-up is off, and `brunna doctor`,
the status page and `brunna run brunna/librarian` say so, with the reason, until you set one up.

## Claude Code's API key

brunna runs Claude Code for its background work (the nightly tidy-up, agents sent to letters, scheduled agent jobs) with
your own Anthropic API key. Make one in the [Claude Console](https://platform.claude.com/settings/keys); `brunna init` asks for it
(not shown) and for the most its runs may cost in a day. Skip it and init lists what stays off; enter it later with
`brunna llm set-key --agent`.

- **Cost.** Anthropic bills the key for what is used; a Claude plan doesn't cover it. Max, Ultra and Team plans include
  [monthly API credits](https://support.claude.com/en/articles/17154008), which you claim by linking a Console organization; the key has to
  be that organization's. Whether they cover runs another program starts isn't said there.
- **The budget.** Each run reserves what it may cost before it starts and is stopped there; the nightly tidy-up has its share
  first; what doesn't fit waits, a letter or a job's turn with it (the weekly report's analysis is skipped that week). It counts by the tool's own estimate and stops between
  requests, so a day can go a little over: set a spend limit in the Console too. `brunna llm usage` shows what was used.
- **What one run may cost** is set by kind of work (an agent run: $0.25 fast, $1 ordinary, $4 strong, such as folding old notes; an answer:
  $0.10, $0.50, $2); `llm.budget.caps.<point>` in `brunna.json` changes one. Work whose cap the day's budget never has room for is named by
  `brunna doctor`: folding old notes (an optional module) needs a little over $6.67 a day, or a lower cap.
- **A project's own agent jobs and model calls** need your leave per project (`metered_models: true` in its registry entry).
- **Lending the conversation to your assistant** doesn't run on a key yet.
- **The key** is in brunna's secrets folder, read only by you, and in the encrypted backup. Claude Code is handed it on a pipe: what a run
  starts doesn't see it. brunna itself uses it once an hour to ask Anthropic whether it works (its list of models, which costs nothing), and
  puts it in a throwaway folder for the checks of a new version. A run with a shell in a project that isn't sandboxed runs as you and could read the file.

## Requirements

- macOS (launchd) or Linux (systemd user units). Project sandboxes use Seatbelt on macOS and Landlock on Linux (a
  declared listening port needs Landlock ABI 4, kernel 6.7 or later). The capability runner, which runs programs a
  sandbox blocks, uses Seatbelt, so it is macOS only.
- `git` and [`uv`](https://docs.astral.sh/uv/). `uv` builds the platform's own Python 3.13 venv, so platform scripts
  don't depend on whichever `python3` is on PATH.
- **A model for the platform's judgment calls**, for the core payoff: the nightly digest, the upkeep of each `STATE.md`,
  and the items in `todo.md` (which are read from the to-do sections the model keeps in `STATE.md`) are all written by
  a model. It can be your coding agent (with its key), an Anthropic or OpenAI-compatible API key, or a local Ollama; pick
  one with `brunna llm setup` (`brunna init` asks). Without one, the platform still runs: project memory folders,
  the mailbox, the scheduler and its jobs, the status page and the project registry all work, judgment calls fail and
  say why, nothing is written overnight, and init leaves the modules that only do model work (consolidation, reflection) off.
- Optional:
  - a coding agent, such as [Claude Code](https://claude.com/claude-code) or any headless CLI agent. Without one, init
    sets `runtime.default: none`: agent features (dispatching agents to mailbox requests, session hooks) are then off,
    and `brunna doctor` lists each one. Claude Code needs [nvm](https://github.com/nvm-sh/nvm) with a default node
    (`nvm install --lts`): brunna keeps a checked copy of it under nvm's npm, and looks for node nowhere else;
  - `gh`, logged in (`gh auth login`) before `brunna init`, to create GitHub repos on onboarding (a folder with no history
    yet starts with an empty commit: none of its files is committed or published until you or an agent does): init reads
    `github.owner` from it. Without it, `brunna onboard` keeps a new project on this machine and says so (`--public`
    fails instead, before anything is made); set `github.owner` in `brunna.json` later to have repos created;
  - `jq`, only on a remote machine whose shell scripts run without the platform's Python;
  - `restic`, for the backup module;
  - an alert channel: Discord, ntfy, email or macOS notifications;
  - Docker, for `bin/linux-test`.

## The daily look for a new release

On a checkout at a release, brunna looks once a day whether a newer one is out (the job `brunna/release-check`; `brunna
doctor` does the same lookup when the last is older than a day). It asks the checkout's own git remote, the place you
installed from, for its tags (`git ls-remote`) and, when there is a newer release, fetches that one tag into a
temporary folder to read whether its changelog lists a security fix. Git sends nothing of yours, and nothing that is fetched is run or checked out.
A checkout on `main` or with work of its own isn't looked up by the job.

A newer release then shows in `brunna glance`, on the status page, in the Claude Code and herdr mods, and in one line to
one of your agent sessions a day (the agent is told to tell you, never to upgrade). A release with a security fix also goes
to your alert channels, once. Nothing upgrades by itself: you run `brunna upgrade`.

To turn it all off, in `brunna.json`: `"updates": {"check": false}`. `brunna upgrade --check` still looks when you run it.

## A machine that sleeps

An always-on machine is recommended, not required. On a laptop, or a desktop set to sleep, brunna catches up when
it wakes; `brunna doctor` says when it sees a machine that sleeps.

| While the machine sleeps | What happens |
|---|---|
| Daily jobs (tidy-up, digest, to-dos, backup) | Run when it wakes, the same day |
| Weekly and monthly jobs (weekly reflection, backup check) | Run at the first tick after waking, if their day was missed |
| Backup retention (Sundays) | Applied at the first backup after a missed Sunday |
| Mailbox dispatch | Paused; letters wait |
| Background services (bots, webhooks, viewers) | Offline: the one real difference |
| Reaching sessions from a phone or the assistant | Unavailable |
| Event-triggered jobs | Handled after waking |

The watchdog doesn't raise a false alarm on waking (on Linux it waits for the scheduler's first tick after a resume).

## Without launchd or systemd

`brunna init` installs the scheduler (a tick every 15 minutes) and its watchdog as launchd agents on macOS or systemd
user units on Linux, then applies the config, which installs the platform's background services (the status page,
mailbox dispatch, memory sync). `brunna init --no-launchd` does neither: use it in a container or on a machine you are
only trying the platform on. A system with neither launchd nor a systemd user session (many containers, WSL without
systemd) ends up the same way, and init and `brunna doctor` say so.

Then nothing runs by itself. Run a single job now with `brunna run <job>` (`brunna jobs` lists them), or schedule
the scheduler's tick yourself, every 15 minutes, with cron or similar: `<hub>/bin/py <hub>/bin/scheduler.py`.
`brunna doctor` shows what is not running.

The status page is a static page. Every scheduler tick rewrites it as `.state/status/index.html` in the hub folder, and
the `local.status-page` service serves it on `127.0.0.1:18790`. Its address has a key in it, which `brunna glance` prints: whoever
has the address can open the page, so bookmark it and don't post it. Without the service, open that file in a browser, or serve
it yourself with `bin/py bin/status-server.py` in the hub folder. Without the scheduler, `bin/py bin/gen-status-page.py` writes it once.

## The status page from another computer

The status page listens only on the machine that runs brunna (`127.0.0.1:18790`). From another computer, either
forward it over SSH and open there the address `brunna glance` prints on that machine (`http://127.0.0.1:18790/<key>/`; write
`127.0.0.1`, not `localhost`, which can be another program):

```bash
ssh -N -L 18790:127.0.0.1:18790 <the hub machine>
```

or, with [Tailscale](https://tailscale.com/) on both, share it on your tailnet only, from the hub machine:

```bash
tailscale serve --bg 18790
```

and open the HTTPS address `tailscale serve status` prints, with the key after it (`https://<that address>/<key>/`; put that
address, without the key, in `brunna.json` as `status_page.url`, and `brunna glance` prints the whole). Don't expose it to the internet: it shows your projects'
names and recent activity.

The key (`status-page-key` in the instance's `secrets` folder) keeps the page from other accounts on the machine and from web
pages that reach this port; it is in the nightly backup with the rest of that folder. To change it, delete the file and restart the
service. What the page is made from are files in the hub and the instance, which other accounts can read where your home folder
lets them in.

## Where things live

The repo holds no user data. `brunna init` creates an **instance directory**, `~/.brunna` by default, and
`<repo>/.hub-home` points to it. It takes a new or an empty folder, never your home folder, one that holds other things
or one inside the hub's; an instance an earlier install left there is used as it is, every setting kept:

| | What | Notes |
|---|---|---|
| `config/` | `brunna.json`, the project registry (`projects/<name>.yaml`), `schedule.yaml` | A local git repo. `brunna config check / plan / apply` |
| `data/` | digests, `todo.md`, review log, weekly reflections | Generated; not in git |
| `secrets/` | API keys, tokens, backup password | Never printed; the registry records names and locations only |

Projects live wherever you choose, `~/workspace/<name>` by default. Their memory bare repos and the librarian's read-only
copies sit beside them; the paths are set in `brunna.json`.

## Uninstalling

```bash
brunna uninstall              # lists what it would remove; nothing changes
brunna uninstall --apply      # removes it
```

It stops this hub's scheduler, watchdog and the background services it generated, and takes the platform's parts out
of each connected project: the block in the rules file, the hooks in the agent's settings, the sandbox rules and the
git hooks it wrote. Your own lines and hooks next to them stay, and the projects' code is never touched. It asks, at
the terminal, before removing brunna from your AI apps' tools (a setting for your user). Your notes stay: the
settings folder (`~/.brunna`), the memory repos and each project's `.agent-memory`, and an install after this one uses
them again. `--purge` removes those too, and they can't be brought back: it asks that on its own, and Enter is no
(`--yes` answers it as well). Your own files in the settings folder stay, and are named; a settings folder an earlier
version put in a folder of yours is left whole, for you to remove brunna's part. What can't be removed is said and the rest goes on. nvm, node, the AI agents, uv and git stay. The last step is yours: it
prints the `rm -rf` for the hub folder.
