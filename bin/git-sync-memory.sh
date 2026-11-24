#!/usr/bin/env bash
# Commits and pushes the notes of the memory checkout it runs in (a project's .agent-memory). It adds/commits only memory/
# and STATE.md, the platform's own two, and never does `git add -A`, which would bundle in changes that are not ready to
# commit.
#
# Nothing to commit: no empty commit; what was committed before and hasn't gone out (offline, a failed push) is pushed, which
# merges what the other side has: a copy that is ahead never waits for its next note. Anything that keeps the notes from
# being committed or pushed is said on stderr, exit 1, or one of hubpaths.sh's MEMORY_* when only a person (or an agent) can change it.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/hubpaths.sh"

# This runs git as the user, so its caller has decided, by a project's name, that that's right here: a sandboxed project's .git is
# its own to write, and its memory is synced as the project (project_git.py sync-memory <name>; docs/design/projects-and-repos.md
# section 4). It doesn't ask again by folder: the answer can be changed by flipping a link, and "not sandboxed" is what a flip gets.

# Only in a memory checkout (hubpaths.sh memory_checkout). A plain folder inside another repo (the hub's own, memory:
# false) would make every git command below act on that repo: on 10-31 a bare `git commit` here committed and pushed
# another session's staged files as "sync memory/STATE.md". That one is quiet; another branch checked out is said.
# What is under way is asked first: a rebase has no branch checked out, and "check it out" would throw it away.
if memory_toplevel . && memory_under_way; then
  # a merge (or the like) is someone's to finish, whoever began it: staging its files would mark the conflicts in them resolved
  hub_say "git-sync-memory: a merge or the like is under way in {path}; nothing is committed or pushed until it is finished. Take the conflict marks out of {files}, then run: {command}" path="$(pwd -P)" files="$("$SCRIPT_DIR/py" "$SCRIPT_DIR/memory_paths.py" unfinished)" command="$(memory_finish)" >&2
  exit "$MEMORY_UNDER_WAY"
fi
if ! memory_checkout .; then
  [ -e .git ] || exit 0
  hub_say "git-sync-memory: {path} is not a checkout of {branch}; nothing was committed or pushed" path="$(pwd -P)" branch="$MEMORY_BRANCH" >&2
  exit "$MEMORY_BRANCH_BAD"
fi
# each path on its own: one that does not exist (no STATE.md yet) makes `git add a b` add nothing at all; -A picks up
# deletions of tracked files too
for p in memory STATE.md; do
  if [ -e "$p" ] || git ls-files --error-unmatch -- "$p" >/dev/null 2>&1; then
    memory_git add -A -- "$p" || exit 1   # git says why (the index locked by one that was killed): a failed sync
  fi
done

# what a memory branch may not hold (a hidden name under memory/, an executable, a link, a rules file's name: bin/memory_paths.py) goes back
# out of the index and stays in the folder, so a Finder .DS_Store never commits; it's said once, on stderr. Can't check: nothing commits
"$SCRIPT_DIR/py" "$SCRIPT_DIR/memory_paths.py" unstage || exit 1

# commit only these two paths, whatever else is staged (bash 3.2: no mapfile). Their names as they are (-z: otherwise git
# quotes a non-ASCII one, and there is no such file to commit), taken literally
files=()
while IFS= read -r -d '' f; do files+=("$f"); done < <(git diff --cached --name-only --no-renames -z -- memory/ STATE.md)
if [ ${#files[@]} -gt 0 ]; then
  memory_git --literal-pathspecs commit -q -m "brunna: sync memory/STATE.md" -- "${files[@]}" || exit 1
elif ! memory_ahead; then
  exit 0
fi
# when the hub machine cannot be reached (the caller has checked), commit locally only; a later round with a
# connection pushes it
[ -n "${AGENT_HUB_OFFLINE:-}" ] && exit 0
"$SCRIPT_DIR/git-safe-push.sh" --memory
