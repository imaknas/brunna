# Everyday commands

```bash
brunna status [project]       # project state, services, STATE.md
brunna state history <project>   # every night's STATE.md; `brunna state restore <project> <date>` brings one back
brunna config plan            # what a config change would do; `brunna config apply` applies it
brunna modules                # optional features; `brunna modules enable <name>`
brunna llm setup              # which provider/model the platform's judgment calls use
brunna jobs                   # all scheduled jobs; `brunna run <job>` runs one now
brunna recover <project>      # new machine or lost folder (never re-onboard an onboarded project)
brunna rename <old> <new>     # dry-run first, then --apply
brunna credentials status     # unattended jobs' sign-ins; a to-do gives the `brunna credentials grant …` to run
brunna credentials check-published   # does brunna's published shared sign-in app match this install's
brunna --version              # the release this checkout is at
brunna upgrade --check        # is there a newer release, and what changes; `brunna upgrade` installs it
brunna push --owner <project> [branch]   # push a sandboxed project's branch yourself (it shows the commits and checks, and asks)
brunna push setup <project>   # what the platform may push for that project's sessions, and its push key
```

Inside a project session, agents use `brunna send / inbox / done …` for the mailbox, `brunna test` to run the
project's declared tests, and `brunna merge-branch` to land their branch. The rules they follow are written into each project's instructions file from `templates/`.

## When a night's STATE.md went wrong

The librarian rewrites each project's `STATE.md` every night with a model. A model can get it wrong, and the same notes
read again would not give back the version you had: every night's version is a commit on the project's memory branch,
so you go back instead.

```bash
brunna state history garden-app                  # who changed STATE.md when (the librarian, a session, a restore)
brunna state restore garden-app 2026-11-01       # the difference to how it was at the end of that day; nothing changes
brunna state restore garden-app 2026-11-01 --apply
```

Your to-do list stays exactly as it is today and everything else goes back to that day; an item you were waiting on
that was in the list that day and was deleted since, still open, comes back. Anything else restore leaves out, it
lists. The preview ends with the command to apply it, `--from` naming the version it was computed from: if `STATE.md`
changes before you run it, it refuses and you preview again.

`--apply` writes the result as a new commit (history is never rewritten) and logs it in `review-log.md`. The librarian
then folds the notes written since that day into the restored file on its next run; add `--keep-checkpoint` when those
notes are what it misread. While a nightly job is writing in the project's memory it refuses at once; try again later.
If a restore is stopped halfway (the machine slept), `brunna state restore garden-app` finishes it. Run it on the hub
machine.

## Pushing a sandboxed project

A sandboxed project's sessions can't reach your credentials, and nobody should run `git push` in its folder: your git
would read that repo's `.git`, which the project can write, and run its hooks and settings as you, outside the sandbox.
So the platform pushes for it (docs/design/platform-push.md).

**From the project's session.** The agent runs, in the project's folder:

```bash
brunna push              # the branch checked out; or: brunna push feature-x
```

It asks the platform to push that branch at its current commit and waits up to 90 seconds (`--wait <s>`, `--no-wait`)
for the answer: pushed (exit 0), refused with the reason and what to do (2), failed (1), or not answered yet (75; the
outcome comes as a letter). Only the project's sessions can ask: the request folder (`<push_dir>/<project>/`, outside
every workspace) is writable from its session sandbox and from nothing else of the project's (its services and jobs,
the platform's runs, `brunna agent`, `cap-run`). Codex sessions in sandboxed projects aren't served yet, so they can't
ask. In a sandboxed project with `push_on_merge: true`, `brunna merge-branch` asks the same way for the default branch.

What the platform pushes is yours to say, per project, in its registry entry (owner-only, like the rest of `enforce`):

```yaml
enforce:
  sandbox: true
  git_push:
    branches: all            # none (the default): nothing; others: every branch but the default; all
    emails: [1234+me@users.noreply.github.com]   # what a pushed commit's author and committer may be
```

`brunna push setup garden-app` sets it up: it makes the platform's push key for that repo (a write deploy key, added
with your `gh`; GitHub only), asks what may be pushed and with which email, writes the field, and sets the repo's
commit identity to that email. On a public remote every branch it may push is published as soon as it lands, and
`others` keeps only the default branch's CI and deploys yours. Before anything leaves, the platform checks every new
commit and file, not only what a diff shows: fast-forward only (no force, tags or deletes), the commit asked for and
nothing newer, no submodules, Git LFS or signed commits, size limits, no change to any forge's CI files unless
`enforce.git_ci`, the commit emails, the built-in secret rules plus gitleaks when installed (8.19 or newer), the values
of the project's declared secrets, and on a public remote no private project name or your home path. The project is
told which check refused and how to fix it; what only you can fix (the branch isn't allowed, no email list, the push
key, the remote's own rules) becomes a to-do for you. The push key is the platform's own, one per repo, never your
credential.

**Yourself.** Anywhere, the owner form:

```bash
brunna push --owner garden-app feature-x     # or no branch: the one checked out
```

It sends only to the remote in the project's registry entry (`paths.origin`). The project's own sandboxed job bundles
the branch; your git then pushes it from a copy brunna keeps under its state folder, never from the project's folder.
Before anything leaves, it shows the commits (author, email, subject), the changed paths, any author email the remote
hasn't seen before, and every check above as a flag. A push that passes every check asks once; `--yes` answers that
question (a script, say). A failed check is pushed past only by your interactive `y` to "push anyway?", never by
`--yes`. Run inside a project's sandbox, it refuses before contacting anything: run it in your own terminal.

It only fast-forwards: no force, no tags, no deletes, one branch at a time. A branch the remote has moved past is
refused: merge or rebase in the project first. The CI check doesn't stop the workflows already there from running the
pushed code (its scripts and tests): read the changed paths before you say yes. A remote with no branches yet is
refused too (it has no default branch to compare with): give it a first commit on the forge, such as a README, then
push. The project's own `origin/<branch>` catches up at its next fetch. Run it again if the remote moved while you
read: it pushes only onto what it showed you.
