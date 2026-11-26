# Questions the owner can answer

A project asks the owner something only they can decide with `brunna send --to <owner> --type question`, or, until
now, `brunna merge-branch --question`. Of the 30 open on 11-26, 27 came from one project, every one through
`merge-branch --question`: most were progress reports ("you haven't looked at X yet", "I set this threshold myself"),
some mixed a decision in, written in the asking session's own words ("hand over by state", "the slots"), with no
background, options or default, and later ones repeated earlier ones, which stayed open. The owner knew something was
waiting and could not tell how to answer it. Asking at merge time invites "anything for the owner?" after every merge.

Three parts:

1. **Questions are asked on their own, and checked while the asker is there**: not at merge time; a small model reading
   only what the owner will see says what a reader without the session's context can't tell, and the asker rewrites it
   once or sends it as it is with a reason the owner sees.
2. **"Look at this" is a review list, not a question**, and a question that replaces earlier ones closes them.
3. **More on a question**: a read-only discussion in the project that starts from the question whole and knows where
   its detail is.

## 1. Asking, and the check

### merge-branch takes no question

`merge-branch --question` is refused before anything is merged, saying to ask with `brunna send --type question` and to
merge without it (a question sent after a merge had no one left to fix it, and its send's result was never read). What
the owner should know goes in `--note` (the session report); what they should read goes on the review list (part 2).
The rules file's merge line loses `--question` and says that `--note` is read by no one as a question.

### What the asker gives

`brunna send --to <owner> --type question --text "…" [--context "…"] [--option id "label"]... [--default …]
[--see <path>]... [--replaces <id>]... [--as-is "why"]`

- `--text`: the decision alone, readable without anything else, since some owner sides show only it.
- `--context`: the background in plain words (what the work is, why it needs the owner). The record's `context`
  (core §4); a letter carries it beside the text. Owner sides that show only the text get it appended.
- `--see`: a file in the project with the detail (relative, inside the project; nothing is read). Carried in the
  context's text ("Detail: docs/x.md"), so every owner side shows it.

### The check

Judgment point `question-check` (`completion`, `model_group: fast`, JSON out, `max_attempts: 1`, 45 s). **The platform
builds its prompt** from the question's fields alone (text, context, options, default, see names, replaces), the
project's name and registry description, and this project's open questions read from the bus (the newest 30, as labels
q1…qn, each cut to 200 characters; the model never sees an id). Nothing else from the asker reaches the prompt. It
answers in quotes and fixed values only:

    {"decisions": [quote], "reviews": [quote], "defects": [{"kind": K, "quote"?: quote}], "repeats": [label]}

- `decisions`: spans that ask the owner to decide; `reviews`: spans that ask only to look at or know something.
- `defects`, kind from a fixed list. Absence, naming the part that isn't there, no quote: `no_work` (what the work is
  and why it needs the owner isn't said), `no_choice` (what is to be decided isn't said), `no_detail` (it needs more than
  it says and points nowhere). Spans, with the quote: `unexplained` (a term the owner can't know), `no_consequence` (an
  option whose result isn't said).
- `repeats`: labels of open questions this one asks again.

Every quote must be a span of the fields sent, else that item is dropped; the platform writes every word the asker sees
(fixed text per kind, with the quote), so the answer can't carry anything else back. It decides: refused when
`decisions` is empty, when `defects` isn't, or when `repeats` names questions `--replaces` doesn't; sent otherwise. With
both `decisions` and `reviews`, it is sent and the asker is told the review part belongs on the review list. A refusal
says, per case:

- no decision: "this asks nothing to decide: put it on the review list (`--type review`) or in your notes";
- defects: one line each, with its quote;
- repeats: "this asks again what <ids> asked: add `--replaces <ids>`, or `--as-is` saying why it is new";

and ends with the command as sent plus the flags it lacks, ready to edit. Sent again changed, it is checked again (a
second refusal in the same session leads with `--as-is`); `--as-is "why"` sends it as it is, unchecked, and the owner
sees "sent without passing the check: <why>". Refusals are said to the asker only; nothing is written.

### Where it runs

Only where the asker is there to read a refusal, and never inside a bus transaction or the dispatch tick. A send is
checked when it runs in a session (the runtime names one) and isn't a platform run (no run point, not confined, no
letters of a run): lease handovers, propose, confined mail tools and dispatch taking an outbox send as today. The rule
can be forged; the check is no boundary, so that is fine.

- An unsandboxed project's `brunna send` (or the MCP server's `send` for one, whose hubbus timeout is raised to 150 s):
  in the command, before the letter is written, through `llm.answer`.
- A sandboxed project's `brunna send`: a request of kind `question-check` to the capability runner (`cap-runner.py`)
  carrying only the fields. The runner answers it itself, outside the project's profile: every sandboxed project is
  eligible for this kind whatever it declares, it takes no job slot (the runner answers it on a thread of its own, one
  at a time per project, so a long job doesn't hold it and it doesn't hold the runner's scan), the runner builds the
  prompt and counts the day's checks. The session waits up to 75 s, then writes the outbox file. The runner is the sandbox module's; without it the
  question is sent unchecked and says so. What the sandbox says was checked is trusted, for the same reason.

`--type task` to the owner gets the same check, advisory only (its lines are printed, it is sent): a decision sent as a
task still reaches the owner.

### When it can't run

A question is never lost to the check. No model for the platform's judgments, the point turned off, the budget
refusing, a timeout, an error, an answer outside the schema, or the project's 20 checks of the day used up (counted by
the platform: the CLI's state for an unsandboxed project, the runner for a sandboxed one): it is sent,
and the sender is told in one line that it wasn't checked and why. The question's text goes to the point's model as
the project's notes already do to the librarian's.

`questions.check` in `brunna.json`: `refuse` (default), `advise` (print the lines, send anyway) or `off`.

Scored on 11-26 before shipping at `refuse` (the point's fast model, on the real questions open that day and six
self-contained ones written for it): the open questions, all one-liners from merges, 27 of 28 refused (the one sent asks
a real decision and was told its review part belongs on the review list); the self-contained ones, 1 refusal in 18 at
first (an option's result called unsaid), then 0 in 24 once the prompt says an option whose label says what it does is
fine.

## 2. Review list and replacing

### Review list

`brunna send --to <owner> --type review --text "…" [--see <path>]... [--rev <n>]`: the **whole** current list of
what the owner should look at in this project, each time (the agent rewrites it; old lines drop off). It is an owner
to-do, key `review-<project>[-<n>]`: sent again it amends the open one (terms_rev + 1, as `send_record` does for a task
sent under its key). While one is open, a send must name its revision with `--rev`: one without it, or with an older
one, is refused with the list as it stands and its revision, so a session never drops lines it hasn't seen; the
revision is compared and the amend written under the bus's write lock. `brunna review` prints the open list and its
revision. Once the owner side settles it (the settlement view decides), the next send raises the next key, whatever
`--rev` it names. A sandboxed project's send is compared against its view first and against the bus when dispatch takes
it in; refused there, it comes back as a `request` to the session that sent it, with the list as it stands, so it isn't
lost. It needs
records: with `mailbox.records: false` the type is refused and says why. The type `review` is added to the hubbus
contract (an added enum value).

### Replacing

`--replaces <id>` (repeatable) on a question: each id must be an open question this project asked, else the send is
refused before anything is written. The new question is sent, then each old one is closed the way a project withdraws
a question today (`drop --question <id>`, note "replaced by <new id>"), with records on or off.

## 3. More on a question

### Lease

`POST /v1/leases` takes an optional `question: <key>` (an added field of lease-runner.v2; error `question_unknown`, 409,
when it isn't an open question of the lease's project). The lease is a fresh discuss lease as today; its preamble adds
the question whole (its text, which holds a session's context and files, options, default, the day it was asked, its id), every line quoted as data, and a fixed instruction:
read what it points to and that day's memory note, explain it to the owner in plain words with what each option leads
to and the recommendation, and say when the owner has decided.

The lease keeps `question` in its saved state, so a runner restart keeps it. It doesn't branch the asking session: those sessions are usually past the resume limit by the time a question waits,
their worktrees go, and a branched lease would break the lease's own session id.

The handover (lease-handover.v1) gains an optional `answered: {question, answer}` (an added field). The preamble tells
the session to read the answer back to the owner and fill `answered` only once they confirm it. The runner accepts it
only for the lease's own question, and sends `done --question <key>` as the project with the answer as its text,
checking the send's result (a failure is a `job.failed`, and the question stays open); the handover itself already
reaches the project's mailbox, so the project learns the answer.

Leases don't run on an API key login yet, so neither does this.

### Without the assistant

`brunna question <id>` (owner): the question whole (its text with context and files, options, default, project, when) and
where its detail is. `brunna open-questions` and the owner side's list show the first line and the id.

### The assistant's side

A letter to the assistant's project, not a change here: show a question's context; when the owner asks for more about
one, open a discuss lease with `question`; send a lease's answer in the handover's `answered`.

`session.report`'s `open_questions` stays in the contract, empty, and is marked as no longer filled.

## Out of scope

The questions open now stay as they are; once this lands, the project they came from can be asked to fold them with
`--replaces` and one review list.
