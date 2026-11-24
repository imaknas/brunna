# Sourced by shell scripts: where the instance directory is (same rules as hubconf.config_dir/data_dir/secrets_dir,
# docs/design/new-user.md 3.1), using only shell + plutil (macOS) / jq (other systems): the remote machine (the laptop)
# doesn't necessarily have the platform's Python.
#   HUB_ROOT     the platform (this repo)    HUB_CONFIG  config (hub.json, registry, mirror-list.txt, services.tsv)
#   HUB_DATA     data (digest/, reflect/, todo.md, index.md, review-log.md…)    HUB_SECRETS  credentials
#   HUB_JSON     full path of hub.json; hub_json_get <dotted key> reads one value (prints nothing, non-zero if absent)
#   HUB_MAIN     the main checkout (HUB_ROOT, or for a linked worktree of the hub the checkout it belongs to: the
#                machine's .hub-home, .state and .logs are there, as in hubconf)    HUB_STATE   <main>/.state
# <hub>/.hub-home holds the instance directory → <instance>/{config,data,secrets}; no such file = the old layout
# (everything in the hub root).
HUB_ROOT="${AGENT_HUB_ROOT:-${BRUNNA_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
HUB_MAIN="$HUB_ROOT"
# a linked worktree: .git is a file "gitdir: <dir>", and <dir>/commondir leads to the main checkout's .git (read as
# plain files, no git; an explicit AGENT_HUB_ROOT is taken as it is)
if [ -z "${AGENT_HUB_ROOT:-${BRUNNA_ROOT:-}}" ] && [ -f "$HUB_ROOT/.git" ]; then
  _hp_gd="$(sed -n '1s/^gitdir: *//p' "$HUB_ROOT/.git")"
  case "$_hp_gd" in /*) ;; *) _hp_gd="$HUB_ROOT/$_hp_gd" ;; esac
  if [ -f "$_hp_gd/commondir" ]; then
    _hp_cd="$(head -1 "$_hp_gd/commondir")"
    case "$_hp_cd" in /*) ;; *) _hp_cd="$_hp_gd/$_hp_cd" ;; esac
    _hp_cd="$(cd "$_hp_cd" 2>/dev/null && pwd -P)"
    [ "${_hp_cd##*/}" = .git ] && HUB_MAIN="${_hp_cd%/.git}"
  fi
fi
HUB_STATE="${AGENT_HUB_STATE:-${BRUNNA_STATE:-$HUB_MAIN/.state}}"
_hp_home="${AGENT_HUB_HOME:-${BRUNNA_HOME:-}}"
[ -n "$_hp_home" ] || { [ -f "$HUB_MAIN/.hub-home" ] && _hp_home="$(head -1 "$HUB_MAIN/.hub-home")"; }
_hp_home="${_hp_home/#\~/$HOME}"
if [ -n "$_hp_home" ]; then
  HUB_CONFIG="${AGENT_HUB_CONFIG:-${BRUNNA_CONFIG:-$_hp_home/config}}"; HUB_DATA="${AGENT_HUB_DATA:-${BRUNNA_DATA:-$_hp_home/data}}"
  HUB_SECRETS="${AGENT_HUB_SECRETS:-${BRUNNA_SECRETS:-$_hp_home/secrets}}"
else
  HUB_CONFIG="${AGENT_HUB_CONFIG:-${BRUNNA_CONFIG:-$HUB_ROOT}}"; HUB_DATA="${AGENT_HUB_DATA:-${BRUNNA_DATA:-$HUB_ROOT}}"
  HUB_SECRETS="${AGENT_HUB_SECRETS:-${BRUNNA_SECRETS:-$HUB_ROOT/.secrets}}"
fi
# the settings file: whichever spelling exists, this release's when neither does (a copy of bin/names.py's
# config_file pair, current first; tests/test_name_pairs.py compares it)
NAMES_config_file="brunna.json hub.json"
HUB_JSON="$HUB_CONFIG/${NAMES_config_file%% *}"
for _hp_n in $NAMES_config_file; do
  [ -e "$HUB_CONFIG/$_hp_n" ] && { HUB_JSON="$HUB_CONFIG/$_hp_n"; break; }
done
# the platform's own name and its unit labels' prefix, in both spellings (copies too, compared the same way): for
# scripts that read names a person wrote down, such as services.tsv
NAMES_product="brunna agent-hub"
NAMES_unit="local.brunna local.agent-hub"
# macOS uses the built-in plutil; elsewhere the platform's Python (bin/py), and jq only where that can't run (the remote
# machine before bin/setup-python): so the hub machine never needs jq. Either way: no such value (or not a single value)
# → print nothing, return non-zero. The Python reader prints what jq -r would: strings as they are, numbers as JSON,
# true/false.
_HP_JSON_GET='import json, sys
try:
    v = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(3)
for k in sys.argv[2].split("."):
    if not isinstance(v, dict) or k not in v:
        sys.exit(3)
    v = v[k]
if isinstance(v, bool):
    print(str(v).lower())
elif isinstance(v, (int, float)):
    print(json.dumps(v))
elif isinstance(v, str):
    print(v)
else:
    sys.exit(3)'
hub_json_get() {
  if command -v plutil >/dev/null 2>&1; then
    plutil -extract "$1" raw -o - "$HUB_JSON" 2>/dev/null
    return
  fi
  local out rc
  # -I: -c puts the current folder first on the import path, and a hook's is whatever the session is in
  out=$("$_hp_bin/py" -I -c "$_HP_JSON_GET" "$HUB_JSON" "$1" 2>/dev/null); rc=$?
  case $rc in
    0) printf '%s\n' "$out"; return 0 ;;
    3) return 1 ;;   # read fine, no such single value
  esac
  # the platform's Python can't run here: jq, if there is one
  command -v jq >/dev/null 2>&1 || return 1
  jq -er --arg k "$1" 'getpath($k | split(".")) | select(type == "string" or type == "number" or type == "boolean")' \
    "$HUB_JSON" 2>/dev/null
}
# The name of a project's git remote for its memory bare repo on the hub machine (the .agent-memory branch's upstream).
# Older checkouts called it mini; sync-local-setup.sh renames those.
MEMORY_REMOTE=memory
# The memory branch's name (memory_paths.BRANCH).
MEMORY_BRANCH=agent-memory
# what a memory sync exits with when trying again won't help and somebody has to do something (memory_paths has the same
# numbers; memory_health.py says what to do for each)
MEMORY_CONFLICT=6     # both sides changed the same notes
MEMORY_UNDER_WAY=7    # a merge (or the like) somebody has to finish
MEMORY_REMOTE_BAD=8   # the branch follows a remote that isn't the platform's memory repo, or none
MEMORY_BRANCH_BAD=9   # another branch is checked out there
# git for the notes: never the project's hooks (.agent-memory is a worktree of its repo and shares core.hooksPath: husky
# running npx lint-staged kept notes uncommitted on 2026-10-01, or rewrote them), and nothing left in a stash. On the
# command line, which every git takes (GIT_CONFIG_COUNT needs 2.31)
memory_git() { git -c core.hooksPath=/dev/null -c merge.autoStash=false -c rebase.autoStash=false "$@"; }
# a merge, rebase, cherry-pick or revert under way in the folder git runs in, or conflicts in its index: never touched, whoever
# began it. Every writer of a memory checkout or copy asks this first, under the lock (memory_paths.under_way is the same)
memory_under_way() {
  local f
  for f in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
    [ -e "$(git rev-parse --git-path "$f" 2>/dev/null)" ] && return 0
  done
  [ -n "$(git ls-files -u 2>/dev/null | head -1)" ]
}
# how what is under way here is finished, for a message: after the conflict marks are out of its files
memory_finish() {
  local how="commit -a -m merge" here; here="$(pwd -P)"
  if [ -e "$(git rev-parse --git-path rebase-merge)" ] || [ -e "$(git rev-parse --git-path rebase-apply)" ]; then how=rebase
  elif [ -e "$(git rev-parse --git-path CHERRY_PICK_HEAD)" ]; then how=cherry-pick
  elif [ -e "$(git rev-parse --git-path REVERT_HEAD)" ]; then how=revert; fi
  case "$how" in commit*) ;; *) how="add -u && git -C '$here' $how --continue" ;; esac
  printf '%s' "git -C '$here' $how"
}
# the checkout brought to its upstream when that is all there is to do (arguments: git's, before the command). Never `pull`:
# with pull.rebase set, git 2.30's --ff-only rebases. An ssh that can't connect gives up within a hook's time (unless the
# user's settings say how to ssh). 2: the other side couldn't be asked
memory_ff() {
  if [ -z "${GIT_SSH_COMMAND:-}${GIT_SSH:-}$(git config --get core.sshCommand)" ]; then
    GIT_SSH_COMMAND="ssh -o ConnectTimeout=10" memory_git "$@" fetch -q || return 2
  else
    memory_git "$@" fetch -q || return 2
  fi
  memory_git "$@" merge -q --ff-only '@{u}'
}
# whether the folder's notes have something to send: commits its upstream lacks, or no upstream to compare with (none set,
# or its tracking ref is gone), which is not the same as nothing to send
memory_ahead() {
  local out
  out=$(git rev-list -n 1 '@{u}..HEAD' 2>/dev/null) || return 0
  [ -n "$out" ]
}
# text git printed, for a log or a record: no user name or password of a URL in it
scrub_urls() {
  LC_ALL=C sed -E -e 's#([A-Za-z][A-Za-z0-9+.-]*://)[^/[:space:]]*@#\1#g' -e 's#(^|[^A-Za-z0-9/])[^][:space:]/@'"'"'"]+@([A-Za-z0-9.-]+:)#\1\2#g'
}
# memory_checkout <folder>: whether it is a memory checkout: its own git toplevel (asked of git as the prefix, so two
# spellings of one path are no difference), with the memory branch checked out. Asked before any git that commits, pulls
# or pushes there: in a plain folder git works on the repo around it, the user's own, and a checkout of another branch is
# somebody's work. memory_paths.is_checkout is the same for the platform's Python.
memory_toplevel() { [ "$(git -C "$1" rev-parse --is-inside-work-tree --show-prefix 2>/dev/null)" = true ]; }
memory_checkout() {
  memory_toplevel "$1" \
    && [ "$(git -C "$1" symbolic-ref -q --short HEAD 2>/dev/null)" = "$MEMORY_BRANCH" ]
}
# exclude_add <info/exclude file> <pattern>: the pattern as its own last line (a file may end without a newline, and the
# user's last pattern must not take ours in)
exclude_add() {
  mkdir -p "$(dirname "$1")"
  # counted as bytes: the last one may be part of a character (macOS's tr refuses it in a UTF-8 locale)
  if [ -s "$1" ] && [ "$(LC_ALL=C tail -c 1 "$1" | LC_ALL=C tr -d '\n' | wc -c)" -gt 0 ]; then echo >>"$1"; fi
  printf '%s\n' "$2" >>"$1"
}
# The machine that runs the hub (hosts.server; the old key memory_host); empty = one machine does everything
hub_server() { hub_json_get hosts.server || hub_json_get memory_host; }
# Owner-facing text in the UI language (docs/design/i18n.md): hub_msg "<English msgid>" [name=value …] prints it without
# a newline, hub_say adds one. The domain is the file the call is in (BASH_SOURCE), so its catalog is
# locales/<lang>/<that file's name>.json, looked up by bin/i18n.py. Without the platform's Python (the remote machine,
# before bin/setup-python) the English msgid comes out, its {name} placeholders filled. The msgid is a plain "…" literal
# (no $, no backquote): tests/test_i18n_catalog.py reads it from the source.
# hub_content is the same for content (what goes into a project's or the data dir's files): the content language.
_hp_bin="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_hub_msg() {
  local mode="$1" dom="${2##*/}" id="$3" out kv; shift 3
  if out="$("$_hp_bin/py" "$_hp_bin/i18n.py" "$mode" "$dom" "$id" "$@" 2>/dev/null)"; then
    printf '%s' "$out"; return 0
  fi
  for kv in "$@"; do id="$(_hp_replace "$id" "{${kv%%=*}}" "${kv#*=}")"; done
  id="$(_hp_replace "$id" "{settings}" "${HUB_JSON##*/}")"   # the settings file's name (bin/i18n.py NAMES)
  id="$(_hp_replace "$id" "{{" "{")"; _hp_replace "$id" "}}" "}"
}
# literal replace-all: ${s//pat/rep} treats rep differently in bash 3.2 (quotes kept) and 5.2 (& is the match)
_hp_replace() {
  local s="$1" out=""
  while [[ "$s" == *"$2"* ]]; do out+="${s%%"$2"*}$3"; s="${s#*"$2"}"; done
  printf '%s' "$out$s"
}
hub_msg() { _hub_msg msg "${BASH_SOURCE[1]}" "$@"; }
hub_say() { _hub_msg msg "${BASH_SOURCE[1]}" "$@"; echo; }
hub_content() { _hub_msg content "${BASH_SOURCE[1]}" "$@"; }
