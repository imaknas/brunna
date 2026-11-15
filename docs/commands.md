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
brunna push <project> [branch]   # push a sandboxed project's branch (it shows the commits and asks first)
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

A sandboxed project's sessions can't reach your credentials, so its pushes are yours. Don't run `git push` in its
folder: your git would read that repo's `.git`, which the project can write, and run its hooks and settings as you,
outside the sandbox. Run this instead:

```bash
brunna push garden-app feature-x     # or no branch: the one checked out
```

It sends only to the remote in the project's registry entry (`paths.origin`). The project's own sandboxed job bundles
the branch; your git then pushes it from a copy brunna keeps under its state folder, never from the project's folder.
Before anything leaves, it shows the commits (author, email, subject), the changed paths, any change to `.github/`
and any author email the remote hasn't seen before, and asks. `--yes` skips the question (a script, say).

It only fast-forwards: no force, no tags, no deletes, one branch at a time. A branch the remote has moved past is
refused: merge or rebase in the project first. A commit that changes `.github/` (CI, which runs with the repo's secrets)
is refused unless the project's registry entry has `enforce: {git_ci: true}`. That gate doesn't stop the workflows
already there from running the pushed code (its scripts and tests), and it doesn't look at other services' CI files
(`.gitlab-ci.yml`, `.circleci/`, `Jenkinsfile`…): read the changed paths before you say yes. A remote with no branches
yet is refused too (it has no default branch to compare with): give it a first commit on the forge, such as a README,
then push. The project's own `origin/<branch>` catches up at its next fetch. Run it again if the remote moved while you
read: it pushes only onto what it showed you.
