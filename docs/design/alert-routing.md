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

## Step 2: route by who can fix it (designed, not built)

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

Separate: the login check's two sources sending the same problem twice (`check-claude-login` ids).
