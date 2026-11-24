#!/usr/bin/env bash
# Resolve "push rejected because the remote is ahead" automatically by retrying with a pull (merge).
#
# Background: repos like agent-hub get written from two sides at once -- the laptop (scripts edited by hand) and the
# hub machine (librarian/hooks writing automatically) -- so a plain `git push` is often rejected because the other
# side happened to push first, and each time a person had to pull+merge before pushing again. This script handles the
# common case of "divergent history but no real content conflict" automatically.
#
# A real content conflict (both sides changed the same part of the same file, and merge cannot combine them) is never
# settled by guessing which side to keep with some strategy -- it prints the error and exits non-zero; that case is
# meant for a person to look at.
#
# Usage: call it after cd-ing into the target repo, e.g. git-safe-push.sh or git-safe-push.sh origin main.
#   git-safe-push.sh --memory   the memory branch, in a memory checkout (hubpaths.sh memory_checkout; refused elsewhere)
# The memory branch (--memory, or no arguments with that branch checked out: the librarian's copies) goes by name to its own
# remote, branch.<memory branch>.remote: a bare push goes to remote.pushDefault or branch.<name>.pushRemote when the user
# has set one, and the notes are private. No remote: not pushed anywhere. In a project's .agent-memory that remote must be
# the platform's memory repo by its URL (project_git.memory_remote: one `git push -u origin` in there changes it); the
# platform's own clones have their origin. A merge it makes that conflicts is undone, so the checkout stays usable and later
# notes still commit, and said; that one only, right when its own git merge says it conflicted: any other merge under way is
# never touched. Every other caller's is left as git leaves it. Exit MEMORY_* (hubpaths.sh): only a person (or an agent) can
# change it.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$here/hubpaths.sh"

memory=""
if [ "${1:-}" = --memory ]; then
  shift
  memory_checkout . || { hub_say "git-safe-push: {path} is not a checkout of {branch}; nothing was pushed" path="$(pwd -P)" branch="$MEMORY_BRANCH" >&2; exit "$MEMORY_BRANCH_BAD"; }
  memory=1
elif [ $# -eq 0 ] && [ "$(git symbolic-ref -q --short HEAD 2>/dev/null)" = "$MEMORY_BRANCH" ]; then
  memory=1
fi
push=(git push -q)
err_of() { cat; }   # what a failed git printed, as it is (the memory branch's: without a URL's user or password)
under_way() {
  hub_say "git-safe-push: a merge or the like is under way in {path}; nothing is pushed until it is finished. Take the conflict marks out of {files}, then run: {command}" path="$(pwd -P)" files="$("$here/py" "$here/memory_paths.py" unfinished)" command="$(memory_finish)" >&2
  exit "$MEMORY_UNDER_WAY"
}
if [ -n "$memory" ]; then
  err_of() { scrub_urls; }
  remote=$(git config --get "branch.$MEMORY_BRANCH.remote")
  if [ -z "$remote" ]; then
    hub_say "git-safe-push: {branch} has no remote of its own (branch.{branch}.remote); nothing was pushed" branch="$MEMORY_BRANCH" >&2
    exit "$MEMORY_REMOTE_BAD"
  fi
  top="$(git rev-parse --show-toplevel)"
  if [ -f "$top/.git" ] || [ "${top##*/}" = .agent-memory ]; then   # a project's: never its code remote
    "$here/py" "$here/project_git.py" memory-remote "$top" >/dev/null || exit "$MEMORY_REMOTE_BAD"   # it says why
  fi
  memory_under_way && under_way
  push=(memory_git push -q --no-follow-tags "$remote" "HEAD:refs/heads/$MEMORY_BRANCH")
fi

# The memory branch, diverged: what its remote has, merged in. 0: merged, push again
merge_theirs() {
  local said theirs rc files
  said=$(memory_git fetch -q "$remote" "refs/heads/$MEMORY_BRANCH" 2>&1) || { printf '%s\n' "$said" | err_of >&2; exit 1; }
  theirs=$(git rev-parse -q --verify 'FETCH_HEAD^{commit}') || exit 1
  memory_under_way && under_way
  said=$(memory_git merge --no-edit -q "$theirs" 2>&1); rc=$?
  [ "$rc" = 0 ] && return 0
  # 1 is git's word that this merge itself conflicted (it exits otherwise when it refused to begin)
  if [ "$rc" = 1 ] && [ "$(git rev-parse -q --verify MERGE_HEAD)" = "$theirs" ]; then
    files=$(git -c core.quotePath=false diff --name-only --diff-filter=U | paste -sd, - | sed 's/,/, /g')
    if git merge --abort >/dev/null 2>&1; then
      hub_say "git-safe-push: the notes here and the hub machine's both changed {files}; the merge was undone, both sides kept as they were and nothing was pushed. Someone has to merge them: git -C '{path}' pull --no-rebase {remote} {branch}, take the conflict marks out, then git -C '{path}' commit -a -m merge" files="$files" path="$(pwd -P)" remote="$remote" branch="$MEMORY_BRANCH" >&2
      exit "$MEMORY_CONFLICT"
    fi
    under_way
  fi
  hub_say "git-safe-push: the pull could not merge, possibly a real content conflict; a person has to look:" >&2
  printf '%s\n' "$said" | err_of >&2
  exit 1
}

max_attempts=5
attempt=1

while [ $attempt -le $max_attempts ]; do
  push_err=$("${push[@]}" "$@" 2>&1) && exit 0

  if echo "$push_err" | grep -qE "rule violations|GH0[0-9]+|required_signatures|protected branch"; then
    # blocked by a branch protection rule (e.g. PRs required, signatures required), not by divergence;
    # retrying with a pull does not help and only wastes time.
    hub_say "git-safe-push: blocked by a branch protection rule, not by diverged history; retrying will not help:" >&2
    printf '%s\n' "$push_err" | err_of >&2
    exit 1
  fi

  if ! echo "$push_err" | grep -qE "rejected|fetch first|non-fast-forward"; then
    # not caused by divergence (e.g. network down, authentication failed); retrying is pointless.
    printf '%s\n' "$push_err" | err_of >&2
    exit 1
  fi

  if [ -n "$memory" ]; then
    merge_theirs
  elif ! pull_err=$(git pull --no-rebase -q 2>&1); then
    hub_say "git-safe-push: the pull could not merge, possibly a real content conflict; a person has to look:" >&2
    echo "$pull_err" >&2
    exit 1
  fi

  attempt=$((attempt + 1))
done

hub_say "git-safe-push: the push still fails after {n} attempts" n="$max_attempts" >&2
exit 1
