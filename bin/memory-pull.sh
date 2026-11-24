#!/usr/bin/env bash
# At a session's start, in a project's .agent-memory: bring the notes up to date (hub-event's memory_pull,
# worktree-memory.sh; under with-lock.sh). Only in a memory checkout whose branch follows the platform's remote by name.
# Fast-forward when that is all there is to do; with commits here that haven't gone out (offline, a failed push), the
# push merges what the other side has and undoes a conflict, so the session doesn't read old notes until some later sync.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$here/hubpaths.sh"
memory_checkout . || exit 0
case "$(git config --get "branch.$MEMORY_BRANCH.remote")" in "$MEMORY_REMOTE"|mini) ;; *) [ -d .git ] || exit 0 ;; esac
memory_under_way && exit "$MEMORY_UNDER_WAY"
memory_ff; rc=$?
[ "$rc" = 0 ] && exit 0
[ "$rc" = 2 ] && exit 1                  # the other side can't be reached: a push would only wait for it again
[ -n "${HUB_AS_PROJECT:-}" ] && exit 1   # run as a sandboxed project: its sync's bundle carries what isn't out yet
memory_ahead || exit 1
exec "$here/git-safe-push.sh" --memory
