# Alerts go to whoever can fix them (11-18)

The owner's question (11-18): "why are there always strange alerts; is something systemically broken?" Nothing was
broken for long. Of the 72 platform alerts the assistant received from 11-08 to 11-18, about 11 needed the owner (an
expired login, credentials about to expire). The rest:

| Kind | About | Who could fix it |
|---|---|---|
| a platform change broke a live job (render-settings, contracts-check, backup, the memory sync after `safe.bareRepository`) | 20 | the platform's agents |
| a project's job failing for days, sent again and again | 15 | the project |
| tests that reached the owner's channels (fixed 11-14) | 13 | the platform's agents |
| the platform's own checks that stopped testing anything | 6 | the platform's agents |
| only the owner (logins, keys, credentials) | 11 | the owner |
| recoveries and "passed" notices | several | nobody |

Three causes, all the platform's:

1. Every alert goes to the owner, whoever can fix it. It breaks the owner's rule in `direction.md` (complexity lands
   on the platform's agents).
2. The platform changes fast and its gate runs in a clean environment, so what only this machine has shows first as a
   live job failing.
3. One alert for all failing jobs was sent again whenever any line changed, and a job's line names its log, a new
   file each day: one failure came back as new for two weeks. The login problem came from two checks, so it came twice.

## Step 1: stop the repeats (built 11-18)

- One alert per failing job (`scheduler-job:<job>`), the job's name its id: a changed detail is not new, and each job
  recovers alone. The old single key is closed once.
- A recovery goes only to the keyed channels (the assistant closes its item); nothing is pushed to the owner's phone or
  inbox for it. Without a keyed channel, nothing is sent and it counts as told.
- `.state/alerts-log.jsonl`: one line per notice tried (key, state, sent), so whether the alerts got quieter can be
  counted without reading the assistant's data.

## Step 2: route by who can fix it (built 11-18)

- One table in `alert.py`, by key prefix, defaulting to the owner: a new caller with no entry still reaches the owner.
  The call sites don't change.
- First only the clear cases: a project's own scheduled job → a letter to that project; the platform's own jobs and
  self-checks → a letter to brunna. Safety refusals and credentials stay the owner's.
- Where each open problem went is recorded, and its recovery goes only there; problems already open at the assistant
  still get their `resolved`.
- Never silent: a letter that can't be sent falls back to the owner's channels; a problem nobody fixes within a day goes
  to the owner once.
- The letter send is a new way out of a test: behind the same guard as the channels.

Checks before building (each can change the design):
1. What `job.failed --to <project>` already shows a project's sessions (perhaps a second path is not needed).
2. What the headless triage (`templates/prompts/mailbox.md`) does with a request it can't act on: if it asks the owner,
   the alert only comes back as a question.
3. The mailbox caps (6 dispatches per project per day, 4 rounds per thread), and whether brunna can write to itself.
4. Letters to a busy session wait (the dispatch fix is open in the mailbox lane): routing builds on it.

Answers (11-18):
1. `job.failed --to <project>` is a bus event, not a letter: project sessions don't see it. The letter is not a second
   path.
2. The headless triage leaves a request that needs a code change open, with a memory note, for the next session; it
   doesn't ask the owner. So a letter doesn't come back to the owner as a question.
3. The mailbox caps (6 runs, 12 wakes per project per day, 4 rounds per thread): one open request per alert key at a
   time, closed when it's fixed; a job failing every tick must not spend a project's budget.
4. Busy sessions: landed 11-15 in the mailbox lane (an idle session first; a busy one that hasn't acked holds a letter
   10 minutes); "release when busy" was measured and left out. The alert letters are its first real traffic.

The mailbox lane's further points, adopted:
- What watches the mailbox itself (the scheduler watchdog, the dispatch service, a bus that can't be written, hubbus
  broken, an alert that can't be sent) never goes through the mailbox: straight to the owner.
- What only the owner can do (logins, credentials, a token to issue again) goes straight to the owner, not through
  brunna for a day.
- `update-runtimes` and `session_versions` keys go to brunna, a version failing a safety check included; after a day
  without an answer, the owner.
- With no brunna session open the triage can only write notes, so the one-day escalation is what makes the platform's
  letters safe.

As built (`alert.route`, `alert._agent`): `scheduler-job:<project>/<job>` goes to that project when it is a registered
project, else to brunna (its own jobs and the modules'), except `OWNER_JOBS` (the bridge); `PLATFORM_KEYS`
(update-runtimes, session versions, completion tools) to brunna; everything else to the owner, the git fence's
findings and every safety refusal included (security findings are the owner's). A letter is a `request` with a fixed id per opening; a changed list goes to its thread as `info`; fixed,
the platform takes the letter back (`done --withdraw`). A day unsettled, or a letter that can't be sent: the owner is
told, prefixed with which project was told first, and from then on it is an ordinary owner alert (its daily reminder,
its recovery to the assistant, the letter taken back). A problem the owner was told of before routing stays the
owner's. Every step is in `alerts-log.jsonl` (`letter`, `letter-update`, `letter-resolved`, `open`, `reminder`,
`resolved`).

After its cold review: a list that keeps changing (a reason carrying a path or an error tail) is escalated a day on
as well; a take-back that fails is tried again until it goes or the letter turns out closed already; the letter id is
ASCII only (hubbus's id pattern). Left as is: a letter sent from a process inside an agent session (an owner typing
`schedulectl run` in a brunna session) carries that session as its sender's, so that session isn't told it; clearing
it would name one tool's variable in the core.

Separate: the login check's two sources sending the same problem twice (`check-claude-login` ids).

## Step 3: a check that found something hasn't failed (11-24)

On 11-24 `assistant/push-reach` found that the assistant's pushes hadn't reached the phone since 01:16. It alerted the
owner under its own key (`push-reach`, the owner's), as it should. It also exited 1, so the scheduler counted it as a
failed job too:
- a second alert, `scheduler-job:assistant/push-reach`, went as a letter to brunna, whose agents can't fix a phone.
  After a day it would have reached the owner as a duplicate. That breaks step 2: what only the owner can do goes
  straight to the owner;
- it was retried every tick (15 minutes) instead of every hour, each run asking the assistant again;
- `job.failed` went on the bus, and the status page showed red, as if the check were broken.

`check-secrets`, `check-claude-login` and `check-runtime-login` do the same. A run of one of these four now ends in one
of three ways:

- **0**: nothing found.
- **`alert.FOUND` (4)**: the check ran, and every problem it found has reached whoever the routing table names (the
  owner, for these four). `alert.once` returns whether that is so: the problem was sent now, or it is open and was
  told already. The scheduler treats this as a completed run:
  - it is not retried before its next turn, and `last_ok` and the day's mark are set;
  - it does not count as failing: no `scheduler-job:` alert and no `job.failed`. A `scheduler-job:` alert open from
    before recovers, and its letter is taken back;
  - the log line reads `job '<name>' finished, problems found and sent (exit 4)`;
  - the status page shows it amber, as "problems found".
- **anything else** (a crash, a timeout, exit 1): the check is broken. It is a failed job as before, and its alert goes
  to whoever fixes the job. Two kinds of finding also exit 1, so they are retried at the next tick and shown red as
  before:
  - a problem that says the check itself broke: the push health answer breaks the contract, the ingest token can't be
    read, the login check raised, or llm.py printed no JSON;
  - a finding `alert.once` couldn't deliver, because no channel is set up or a send failed.

  Only the code path that called `alert.once` may answer FOUND; `check-secrets <project>` doesn't.

**Who may answer FOUND**: a job that declares `found_exit: true` in its registry entry; these four do. The registry
accepts the field only in the platform's own entry and in modules, so a project's job can't hide its failures behind
it. It is also refused on an event job, where FOUND would have to say what happens to the batch. The scheduler carries
the flag into a background run's file, and `--record-run` and `schedulectl run` look the job up. For any other job a 4
is an ordinary failure, so a tool that happens to exit 4 is never read as "sent".

**Reminders for a daily check**: a reminder is due once 23 hours have passed, not 24. A daily check sends once a day,
and a run that starts a few minutes earlier than yesterday's would otherwise push the reminder to the next day. Until
now, the exit-1 retry hid that.

Left as is:
- `render-services`: some of its problems are its own apply failures. Its key goes to the owner, while a service that
  isn't running is its project's to fix. The `scheduler-job:` letter to brunna is the only way agents hear of them.
- `credentials.py keepalive`: its exit 1 is for errors it sends nowhere.
- `update-runtimes`: its keys and its job's letter both go to brunna, so the duplicate reaches the same reader.
- `glance` reads only failing jobs, so a FOUND run isn't there; the finding itself is the owner's alert.
