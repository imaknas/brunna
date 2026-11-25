#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The project registry: <registry_dir>/<name>.yaml (one file per project, directory set in {settings}) → validation →
JSON cache.

The platform's code holds no project names: a project's jobs, services, privacy labels and paths are all in the
registry. The scheduler calls refresh() on every tick: each file is validated on its own; valid ones are written to
<hub>/.shared/registry/<name>.json (CACHE: the cache of the last valid version; sandboxed code reads it), invalid ones keep using the last valid version and
their errors go into index.json's errors (red on the status page), **without affecting other projects**.
index.json is for programs that may only use the standard library (hubconf.py, hubbus).

schema v1 (contracts/project.v1.md has the full description):
  v: 1                      required
  name                      required, = the file name
  type: project | idea      default project. idea = a project the assistant opens to explore an idea (created by
                            bin/create-idea-sandbox.py; the name is the idea's code name, following the ordinary
                            project-name rules (early ones were called idea-<n>); no GitHub remote;
                            bin/upgrade-idea.py upgrades it to project). Whether it's an idea sandbox depends only on
                            this field, not on the name
  purpose                   the idea itself (a sentence or two); required for type: idea, included in the lease's
                            system prompt
  paths: {workspace, memory_bare, origin}       workspace required (except for the platform itself)
  memory: true              takes part in memory tracking
  privacy: {reflection: include|stats_only|exclude}
  runtime: {deploy, test}   has a runtime = has <services_root>/<name>
  push_on_merge: false
  commits: conventional      commit titles use Conventional Commits: merge-branch checks before merging
                            (bin/commit_title.py), and the rules file automatically carries
                            templates/rules/conventional-commits.md. Not set = no check
  mailbox: {escalate: true}   # and wake: false: a letter never wakes or is routed to this project's sessions (owner-only, PLATFORM_OWNED_NESTED)
  publishes: [{kind, path, contract, v}]
  provides:  [{contract, v, schema}]           schema relative to workspace
  services:  [{name, run, label?, keepalive?, interval_s?, throttle_s?, env?, cwd?, log?, expose?}]
                                  (bin/render-services.py). A name and a label are letters, digits, . _ -, unique in the
                                  registry whatever their case, and never one of the platform's own. A sandboxed project
                                  chooses no label (always local.<project>.<name>), and its unit carries none of its
                                  environment or log: those apply inside its sandbox only (bin/sandboxed-run)
  enforce: {sandbox: false, capabilities: [], work: false, git: platform?, git_ci: false, git_push?}
                                  phase two: sandbox true = render-project-settings.py writes the sandbox +
                                  permission rules; capabilities = platform capabilities ({settings} capabilities,
                                  e.g. gpu), needs sandbox: true; inside the sandbox, hub-run --cap <capability> runs
                                  things through the platform (docs/design/capabilities.md);
                                  work = a discuss lease may make changes in work mode (writing files inside the
                                  folder, commands asked every time), needs sandbox: true;
                                  git: platform = its git goes through the platform (docs/design/git-broker.md): no
                                  sandbox of it writes a git dir (policy git_fence: no .git, no HEAD file anywhere it
                                  writes), needs sandbox: true. Rendered so far into its services', jobs' and cap-run's
                                  Seatbelt profile only; refused on an OS whose sandbox can't fence (Linux).
                                  git_ci: true = brunna push (bin/git_remote.py) may push a commit whose .github/
                                  differs from the remote default branch's (CI runs with the repo's secrets); needs
                                  sandbox: true.
                                  git_push: {branches: none|others|all, emails: [..], public_emails?: [..],
                                  max_file_mb?: 1-100, visibility?: public|private}: what the platform pushes on its
                                  sessions' request (`brunna push`, docs/design/platform-push.md section 5): none (or
                                  absent) nothing, others every branch but the default, all the default too; anything
                                  but none needs sandbox: true, a paths.origin brunna push contacts and the project
                                  out of service_sandbox_exempt. emails: what a pushed commit's author and committer
                                  may be; on a public remote only noreply ones and those in public_emails.
                                  visibility: what `brunna push setup` saw (a live probe that differs is a to-do).
                                  The record of the owner's explicit authorization (10-25): written when an idea
                                  sandbox is created, carried over on upgrade; other projects add it only with the
                                  owner's consent
  models: {<point>: {tier}}       model calls this project makes through the platform (brunna complete; tier fast |
                                  standard | strong, {settings} decides the model; the owner may pin one to a
                                  provider, e.g. a local model, in llm.points.<project>/<point>). Undeclared points
                                  are refused
  metered_models: true            the owner allows metered (pay-per-use) providers for those calls; without it a call
                                  that would go to one is refused (llm.py project)
  agents: {<name>: {tier, allow, timeout?}}
                                  confined agent calls this project's code makes (brunna agent <name> < prompt; run by
                                  cap-runner, judgment point project-agent): tier as models:, allow: the whole boundary
                                  (the same rule strings as a job's; what a run may have is checked when it runs, A2),
                                  timeout 45m / 1h (default 30m, at most {settings} cap_runner.agent_max_timeout_s).
                                  docs/design/project-agent-calls.md A
  agents_per_day: <minutes>       lowers this project's daily minutes of agent calls (cap_runner
                                  .agent_minutes_per_project), never raises them
  network: [domains]              what the sandbox needs to reach beyond the platform defaults
  allow_write: [paths]            what the sandbox needs to write beyond the platform defaults
    type, enforce, network, allow_write, privacy, metered_models and paths are security fields: a manifest.update a project
    sends that changes them is rejected whole (a project can't switch off its own sandbox, widen its scope or add
    capabilities);
    to change them, ask the brunna session (the owner confirms)
  secrets:   [{name, path, env_key?, expires? | max_age_days?, warn_days?, backup?, why?, rotate?, login? (a module's: the login of its
             agent it is, checked only where that login is used)}]
                                  the credentials this project uses: only name, location and expiry, **never the
                                  value** (check-secrets.py checks daily that they exist, are backed up, and aren't
                                  about to expire). path is relative to runtime (if there is one) else workspace, or
                                  ~/ or an absolute path; env_key = the file is a .env and the credential is one line
                                  of it (only checks that the key is there); expires is "YYYY-MM-DD" (quote it);
                                  max_age_days counts from the file's modification time (replacing it restarts the
                                  count); backup: false needs a why (e.g. just reissue it). None of the project's
                                  confined runs reads or writes them (a relative path under both its workspace and
                                  runtime; policy.secret_paths); `registry.py check --secrets-report` lists what that
                                  keeps from which jobs
    kind: oauth | static (a platform credential, docs/design/platform-credentials.md C1): the platform's own grant for a
      remote MCP server, held in its secrets directory (bin/credentials.py places it; no path, env_key or expiry), given
      only to the jobs that list it in credentials:. server: {id, url, header?}: id is the tool prefix the job's rules
      use, url the server's exact https URL, header (static only) the header the value goes in (default Authorization,
      sent as Bearer <value>). backup defaults to false (re-grant after a restore), broken: todo (default; an alert and
      an owner to-do with the re-grant command) | alert. An entry with kind is owner-only (credential_entries), and not
      for modules
  gate: strict                    the merge gate enforced: main in the main checkout can't be committed to directly
                                  (only via merge-branch); by default it only detects
  jobs:      [{name, at | every | trigger, run | (kind: agent + prompt), cwd?, days?, days_of_month?, months?,
               until?, between?, background?, after?, max_attempts_per_day?, timeout?, legacy_name?, catch_up?,
               found_exit?}]
    found_exit: true (a scheduled command job of the platform or a module only): the job is a check that sends what
      it finds itself (alert.once); its exit alert.FOUND (4) means it ran and what it found was delivered, a completed
      run for the scheduler, not a failure (docs/design/alert-routing.md step 3). Any other job's 4 is a failure
    catch_up: period (a job with at and day filters): when the machine was asleep or off on the scheduled day, the
      job runs at the first tick after it wakes instead of skipping to the next scheduled day (newcomer-path.md B7)
    timeout: 45m / 2h: the run is stopped (its whole process group) past this; without it the scheduler's default
    every: 15m / 1h; cwd: runtime (default, when there's a runtime) | workspace | hub | state (agent jobs only: runs
      in its own state directory, without loading the project's settings/MCP)
    trigger: {events: [bus event type], projects?: [self|<project>|*], sources?: [glob], where?: {payload.key: value |
      [values]}, debounce?: 0m/10m/1h} = an event-triggered job (docs/design/event-jobs.md): runs when matching bus
      events wait (the scheduler's tick). Not `on:` (YAML reads it as true). events come from the hubbus contract's
      type enum; message only with where.type limited to info/progress/done (the rest is mailbox work).
      An ordinary project sees only events addressed to it (self, including names from before a rename); the platform
      project and modules may name others or *. Bus events are their writers' claims: the platform's event jobs are
      never agent jobs, and a project's agent event job must declare allow: (its boundary) and run in the workspace
      (cwd: workspace); it runs under judgment point agent-job-event, which loads only the platform's settings for
      the project (no accumulated project or user permissions). Its prompt carries any bus writer's text, so its
      allow: may name only reading and editing files and fetching from the project's own network: domains (no shell,
      no open web), and its run writes no rules file, STATE.md or other memory file but the notes; what its editing
      rules reach is what that text can change, so a rule over the whole folder lets any bus writer change files the
      project's tests and services run as the user. message.delivered, message.routed, message.passed, message.woken and message.done are refused like record.
      max_attempts_per_day defaults to 12; an agent event job always runs as the scheduler's background job, in
      place, so the scheduler sees its outcome. legacy_name is refused
    after: [job] (same project) or [project/job]
    before: [job]: this job first, then that one. A sandboxed project's names only its own jobs (one that names another
      project's is ignored, and said; a manifest.update adding one is refused)
    allow: [rules] (agent jobs only): the job's permission rules, its whole boundary: it runs unattended (dontAsk)
      under judgment point agent-job-confined (agent-job-event when triggered), which loads only the platform's
      settings for the project, never the project's saved permissions, the user's or its hooks. Without allow: an
      agent job runs in auto mode with the project's settings (flagged on the status page)
    connectors: true (agent jobs only): needs claude.ai connectors (Upwork etc.) → uses the subscription login, not
      the default headless token (tools in allow with mcp__claude_ai_ but this not set: invalid). Such a run loads the
      machine's tool servers; every one whose tools allow doesn't name is denied for it (tool_servers: allowed_only)
    mail: true (a scheduled agent job with allow: only, never an event job): its run gets the platform's mail tools to
      start new letters (send a request, question, info or progress; ask the owner; write notes when its rules write
      them), never to read the mailbox, reply or close (docs/design/confined-mail-tools.md M1). Without it a confined job
      sends no mail. On an event job it is refused: any bus writer's text would then send as the project
    credentials: [<name>] (a scheduled agent job with allow: only, in a project that isn't sandboxed): the platform
      credentials (secrets: entries with kind) its run gets, each through the platform's bridge to that server only
      (docs/design/platform-credentials.md C5, C6). Owner-only, and such a job is frozen whole (credential_jobs). Its
      rules name exactly the tools of those servers it calls, and nothing that runs code or reads outside its folders
      (the runtime's credential_tools port says which; a job they don't fit doesn't run)
    tier: fast | standard | strong (agent jobs with allow: only): the model strength its run uses, instead of the
      tool's default (an owner's model pin on the judgment point still wins). Only where the runtime can tell how the
      run is paid for (for Claude Code: the headless token, not the interactive login connectors: true uses); a
      pay-per-use one needs metered_models: true and counts toward llm.budget.daily_usd
      (docs/design/project-agent-calls.md B)
    legacy_name: the old name in schedule.yaml: while the old name is still in schedule.yaml this job isn't used
                 (during the switch the old one wins; delete that entry from schedule.yaml to switch over; the state
                 moves over from the old name too)

schema v2 (contracts/project.v2.schema.json; docs/design/test-waiting.md B1): v1's fields, except that tests leave
runtime. validate_v2() checks it; validate() reads both, a v1 file upgraded to v2 as it is read (tiers.upgrade),
since the runner and the readers switched over (test-waiting step 2.3).
  runtime: {deploy}         only deployment is left (present = has <services_root>/<name>, as in v1)
  code: [glob]              the changed files that count as this project's code, relative to its folder; absent =
                            every file
  inputs: [kind:value]      what changes results without being code: file:<path in the repo outside the project's
                            folders> | config:<file name listed in {settings} tests.config_inputs> | env:<NAME> |
                            cmd:<command whose output counts>
  tests: [{tier, run, when, paths?, jobs?}]
                            tier: a name, unique; run: the command, run in an exported tree ({jobs} = the worker
                            count); when: any of commit (needs commit_gate) | merge | nightly | deploy; paths: the
                            tier runs only when a change touches one of them; jobs: {max?, env?} (max needs {jobs} in
                            run or env)
  commit_gate: true         the repo opts into the platform's commit gate (a repo-level field)
Validation: unknown fields, duplicate names, after pointing at a job that doesn't exist, the first word of
run/deploy/test not found (a file relative to cwd, a command on PATH, or an existing absolute path) all count as
invalid.

Usage: registry.py [check [name...]]   validate and refresh the cache, print the result
       registry.py check --rebuild         rebuild even from another or an empty registry dir (wrong_registry())
       registry.py add <name> <workspace> <origin> <memory_bare> [--extra <JSON>]
                                           add at onboarding (left alone if it exists; extra overrides default
                                           fields; nothing written if validation fails)
       registry.py rename <old> <new> <workspace> <origin> <memory_bare>
                                           rename (the file and the prompt directory move together)
       registry.py apply-updates           apply the manifest.update letters projects sent (platform job, every
                                           15 minutes): a letter with v is the whole entry; one without is the
                                           part that changes, merged into the entry (manifest_update())
"""
from __future__ import annotations

import copy
import datetime
import functools
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import event_jobs  # noqa: E402
import hubconf  # noqa: E402
import names  # noqa: E402
import i18n  # noqa: E402
import isolation  # noqa: E402
import tiers  # noqa: E402

import yaml  # noqa: E402

_ = i18n.domain(__file__)

CACHE = hubconf.REGISTRY_INDEX.parent   # hubconf.SHARED: readable inside every sandbox, written only here
TOP = {"v", "name", "type", "purpose", "paths", "memory", "privacy", "runtime", "push_on_merge", "commits", "mailbox",
       "publishes", "provides", "consumes", "services", "jobs", "enforce", "network", "allow_write", "gate", "secrets",
       "models", "metered_models", "layout_intent", "agents", "agents_per_day"}
TOP_V2 = TOP | {"tests", "code", "inputs", "commit_gate"}
# what the index's documents are: 2 = project v2 (v1 files upgraded as they are read). Readers that need v2 fields
# (merge-branch) refuse an index of an older format; registry.refresh never overwrites a newer one
INDEX_FORMAT = 2
MOMENTS = ("commit", "merge", "nightly", "deploy")
TIER_KEYS = {"tier", "run", "when", "paths", "jobs"}
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
# a secret's path goes into confined runs' deny rules (policy.secret_paths): no glob, bracket, brace or control character
SECRET_PATH = re.compile(r"[^\x00-\x1f\x7f()\[\]{}*?]+")
SECRET = {"name", "path", "env_key", "expires", "max_age_days", "warn_days", "backup", "why", "rotate", "kind", "server",
          "broken", "login"}
CREDENTIAL_KINDS = ("oauth", "static")
CREDENTIAL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")   # a file name and part of an owner to-do's key
SERVER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")         # runtime_ports.ToolServer.id's spelling
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
RESERVED_LOCAL_PORTS = (47615, 18790)   # credentials.CALLBACK_PORT, status_key.PORT: no service may listen there (tests check they agree)
JOB = {"name", "at", "every", "trigger", "run", "kind", "prompt", "cwd", "days", "days_of_month", "months", "until",
       "between", "background", "after", "before", "max_attempts_per_day", "timeout", "legacy_name", "allow",
       "connectors", "catch_up", "mail", "tier", "credentials", "found_exit"}
TRIGGER = {"events", "projects", "sources", "where", "debounce"}
# the mailbox's own work and bookkeeping (inbox records, delivery and closing written by dispatch, which relays others'
# letters without a chain): no event job reacts to them (event-jobs.md 3.5)
MAILBOX_WORK = ("record", "message.delivered", "message.routed", "message.passed", "message.woken", "message.done")
NOTICES = ("info", "progress", "done")   # the only letter types an event job may react to (event-jobs.md 3.5)
SERVICE = {"name", "run", "label", "keepalive", "interval_s", "throttle_s", "env", "cwd", "log", "expose"}
DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
EVERY = re.compile(r"([0-9]{1,6})(m|h)\Z")
BETWEEN = re.compile(r" *([01]?[0-9]|2[0-3]):[0-5]?[0-9] *- *([01]?[0-9]|2[0-3]):[0-5]?[0-9] *")
YEAR_S = 366 * 24 * 3600
# a service's name is part of its unit's file name and label, a label all of it
SERVICE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,100}")
SANDBOXED_SERVICE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,100}")
LABEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,200}")
TIERS = ("fast", "standard", "strong")
MODEL_POINT = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")


def dirs(doc: dict) -> dict[str, pathlib.Path | None]:
    """This project's runtime / workspace / hub directories."""
    name = doc.get("name", "")
    if name == hubconf.hub_project() or doc.get("type") in ("module", "platform"):
        return {"runtime": hubconf.ROOT, "workspace": hubconf.ROOT, "hub": hubconf.ROOT}
    ws = hubconf.path((doc.get("paths") or {}).get("workspace"))
    rt = hubconf.p("services_root") / name if doc.get("runtime") is not None else None
    return {"runtime": rt, "workspace": ws, "hub": hubconf.ROOT}


def runs_sandboxed(doc: dict, stem: str | None = None) -> bool:
    return doc.get("type") not in ("module", "platform") and isolation.sandboxed({**doc, "name": stem or doc.get("name")})


def service_label(project: str, doc: dict, svc: dict) -> str:
    """The launchd/systemd label of a service: its own, or local.<project>.<name>; always that for one that runs
    sandboxed, whatever its entry says."""
    own = None if runs_sandboxed(doc, project) else svc.get("label")
    return own or f"local.{project}.{svc['name']}"


def label_ok(label) -> bool:
    return isinstance(label, str) and bool(LABEL.fullmatch(label)) and ".." not in label


def _fold(label: str) -> str:
    """Labels as a file system that ignores case compares them."""
    return label.casefold()


@functools.cache
def core_labels() -> frozenset[str]:
    """The platform's own labels, in either spelling (folded): its scheduler and watchdog and what else is in launchd/,
    its own entry's services and every module's, on or off."""
    found = {names.unit_label(s, sp) for s in names.CORE_UNITS for sp in names.all("unit")}
    found |= {f.stem for f in (hubconf.ROOT / "launchd").glob("*.plist")}
    for f in [hubconf.platform_registry(), *sorted(MODULES_DIR.glob("*.yaml"))]:
        try:
            doc = yaml.safe_load(f.read_text()) or {}
            for svc in doc.get("services") or []:
                found |= {svc.get("label") or f"local.{n}.{svc['name']}"
                          for n in (names.all("project") if f.parent != MODULES_DIR else [f.stem])}
        except (OSError, yaml.YAMLError, AttributeError, KeyError, TypeError):
            continue
    return frozenset(_fold(s) for label in found for s in names.unit_spellings(label))


def labels(project: str, doc: dict | None) -> dict[str, str]:
    """{folded label: service name} of an entry, as far as its services can be read."""
    out = {}
    for svc in (doc or {}).get("services") or []:
        if isinstance(svc, dict) and isinstance(svc.get("name"), (str, int)):
            svc = isolation.normal_service(svc)
            out.setdefault(_fold(str(service_label(project, doc, svc))), svc["name"])
    return out


def label_clashes(project: str, doc: dict, held: dict[str, dict], known: dict | None = None) -> list[str]:
    """The labels doc takes that another entry has: held = {project: its labels()}. Not one the project had already
    (known, its labels() before): of two that were accepted, neither is the newcomer (render-services decides)."""
    return [_("service {name}: its label is already {other}'s; pick another name", name=n, other=o)
            for label, n in labels(project, doc).items() if label not in (known or {})
            for o, theirs in sorted(held.items()) if o != project and label in theirs]


def default_cwd(doc: dict) -> str:
    if doc.get("type") == "module":
        return "hub"
    return "runtime" if doc.get("runtime") is not None else "workspace"


def resolves(cmd: str, cwd: pathlib.Path | None, search: str | None = None, outside: bool = True) -> str | None:
    """Can the first word be found? If not, return an error message. outside False (a sandboxed project's command): only
    what can be told without looking at a path the project names, or one it may have put a link at: whether it parses,
    and a bare command on the platform's own PATH. What doesn't resolve fails inside its sandbox, where it runs."""
    try:
        words = shlex.split(cmd)
    except ValueError as e:
        return _("the command cannot be parsed: {error}", error=e)
    if not words:
        return _("the command is empty")
    first = words[0]
    if "=" in first and not first.startswith(("/", ".")):  # VAR=x cmd
        return None
    if first.startswith("/"):
        if first.startswith(str(pathlib.Path.home())):
            return _("no absolute paths under the home directory ({first}); make it relative to cwd", first=first)
        return None if not outside or pathlib.Path(first).exists() else _("{path} does not exist", path=first)
    if "/" in first:
        if cwd is None:
            return _("{first}: there is no cwd to look in", first=first)
        return None if not outside or (cwd / first).exists() else _("{path} does not exist", path=cwd / first)
    if search and not outside:
        return None
    found = shutil.which(first, path=search or f"{hubconf.LAUNCHD_PATH}:{os.environ.get('PATH', '')}")
    return None if found or first in ("cd", "test", "[", "true") else _("{first} is not on PATH", first=first)


def validate_secrets(secrets, credentials_ok: bool = True) -> list[str]:
    if secrets is None:
        return []
    if not isinstance(secrets, list):
        return [_("secrets must be a list")]
    errs, seen, ids = [], set(), set()
    for i, sec in enumerate(secrets):
        where = f"secrets[{i}]"
        if not isinstance(sec, dict):
            errs.append(_("{where} must be a mapping", where=where))
            continue
        errs += [_("{where}: unknown field {key}", where=where, key=k) for k in sec if k not in SECRET or (k == "login" and credentials_ok)]
        if "login" in sec and not credentials_ok and not (isinstance(sec["login"], str) and sec["login"]):   # a module's
            errs.append(_("{where}: login must be the name of a login", where=where))
        if "kind" in sec:
            if not credentials_ok:
                errs.append(_("{where}: kind (a platform credential) is for a project's registry entry, not a module",
                              where=where))
            errs += validate_credential(sec, where, ids)
            if sec.get("name") in seen:
                errs.append(_("{where}: name {name} is a duplicate", where=where, name=sec.get("name")))
            seen.add(sec.get("name"))
            continue
        for k in ("server", "broken"):
            if k in sec:
                errs.append(_("{where}: {key} is only for an entry with kind (a platform credential)", where=where, key=k))
        for k in ("name", "path"):
            if not isinstance(sec.get(k), str) or not sec.get(k):
                errs.append(_("{where}: {key} is required", where=where, key=k))
        if isinstance(sec.get("path"), str) and not SECRET_PATH.fullmatch(sec["path"]):
            errs.append(_("{where}: path may not hold a glob, bracket, brace or control character (it is written into "
                          "the deny rules of the project's confined runs)", where=where))
        if sec.get("name") in seen:
            errs.append(_("{where}: name {name} is a duplicate", where=where, name=sec.get("name")))
        seen.add(sec.get("name"))
        if "expires" in sec and not (isinstance(sec["expires"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", sec["expires"])):
            errs.append(_("{where}: expires must be a quoted \"YYYY-MM-DD\"", where=where))
        if "expires" in sec and "max_age_days" in sec:
            errs.append(_("{where}: give only one of expires and max_age_days", where=where))
        for k in ("max_age_days", "warn_days"):
            if k in sec and (not isinstance(sec[k], int) or isinstance(sec[k], bool) or sec[k] <= 0):
                errs.append(_("{where}: {key} must be a positive integer", where=where, key=k))
        if "backup" in sec and not isinstance(sec["backup"], bool):
            errs.append(_("{where}: backup must be true/false", where=where))
        if sec.get("backup") is False and not sec.get("why"):
            errs.append(_("{where}: backup: false needs a why (why it needs no backup)", where=where))
    return errs


def validate_credential(sec: dict, where: str, ids: set) -> list[str]:
    """A secrets: entry with kind: the platform's own grant (docs/design/platform-credentials.md C1). The platform places
    its file, so it has no path; what it is for is server."""
    errs = []
    if sec["kind"] not in CREDENTIAL_KINDS:
        errs.append(_("{where}: kind can only be {kinds}", where=where, kinds=" / ".join(CREDENTIAL_KINDS)))
    if not isinstance(sec.get("name"), str) or not CREDENTIAL_NAME.fullmatch(sec["name"]):
        errs.append(_("{where}: a platform credential's name is lowercase letters, digits and - (it names its file and "
                      "its owner to-do)", where=where))
    for k in ("path", "env_key", "expires", "max_age_days", "rotate"):
        if k in sec:
            errs.append(_("{where}: a platform credential has no {key} (the platform places it and knows its expiry)",
                          where=where, key=k))
    if "backup" in sec and not isinstance(sec["backup"], bool):
        errs.append(_("{where}: backup must be true/false", where=where))
    if sec.get("broken", "todo") not in ("todo", "alert"):
        errs.append(_("{where}: broken can only be todo (an alert and an owner to-do, the default) or alert", where=where))
    server = sec.get("server")
    if not isinstance(server, dict) or set(server) - {"id", "url", "header"}:
        return errs + [_("{where}: server is {{id, url}} (and header, for a static credential)", where=where)]
    sid = server.get("id")
    if not isinstance(sid, str) or not SERVER_ID.fullmatch(sid) or sid.endswith("-run"):
        errs.append(_("{where}: server.id is lowercase letters, digits and -, and doesn't end in -run (the platform's own "
                      "server's)", where=where))
    elif sid in ids:
        errs.append(_("{where}: server.id {id} is a duplicate", where=where, id=sid))
    ids.add(sid)
    if not credential_url_ok(server.get("url")):
        errs.append(_("{where}: server.url is the server's exact https URL, with no user, query or fragment",
                      where=where))
    if "header" in server and (sec.get("kind") != "static" or not isinstance(server["header"], str)
                               or not HEADER_NAME.fullmatch(server["header"])):
        errs.append(_("{where}: server.header is a header name, for a static credential only", where=where))
    return errs


def credential_url_ok(url) -> bool:
    import urllib.parse
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" and bool(u.hostname) and "@" not in u.netloc and not u.query and not u.fragment \
        and "#" not in url and "?" not in url


def credential_entries(doc: dict) -> dict:
    """{name: entry} of a registry entry's platform credentials (secrets: with kind): owner-only, compared whole."""
    return {s.get("name"): s for s in doc.get("secrets") or [] if isinstance(s, dict) and "kind" in s}


def credential_jobs(doc: dict) -> dict:
    """The jobs that get a platform credential (credentials:), whole, with the folder they run in: as connector_jobs, a
    project's letter can't add one, or change its rules, prompt or folder."""
    return {j.get("name"): {**j, "cwd": j.get("cwd") or default_cwd(doc)}
            for j in doc.get("jobs") or [] if isinstance(j, dict) and j.get("credentials")}


def capabilities() -> dict[str, dict]:
    return hubconf.capabilities()


def validate_enforce(doc: dict) -> list[str]:
    enf = doc.get("enforce")
    if enf is None:
        return []
    if not isinstance(enf, dict) or set(enf) - {"sandbox", "capabilities", "work", "git", "git_ci", "git_push"} \
            or not isinstance(enf.get("sandbox", False), bool):
        return [_("enforce can only be {{sandbox: true|false, capabilities: [{cap}], work: true|false, git: platform, "
                  "git_ci: true|false, git_push: {{…}}}}", cap=_("capability"))]
    # git through the platform (docs/design/git-broker.md, "Rollout per project"): no sandbox of it writes a git dir.
    # Owner-only like the rest of enforce; a sandbox is what the fence is rendered into
    if "git" in enf and enf["git"] != "platform":
        return [_("enforce.git can only be platform (or left out)")]
    if enf.get("git") and not enf.get("sandbox"):
        return [_("enforce.git needs enforce.sandbox: true")]
    # an exempt project's services and jobs get no sandbox at all, so nothing would render the fence
    if enf.get("git") and doc.get("name") in (hubconf.get("service_sandbox_exempt", []) or []):
        return [_("enforce.git can't be on for a project in service_sandbox_exempt: its services and jobs run with no "
                  "sandbox")]
    # brunna push of a commit that changes .github/ (bin/git_remote.py): the owner's allowance, owner-only like the rest
    if not isinstance(enf.get("git_ci", False), bool):
        return [_("enforce.git_ci must be true or false")]
    if enf.get("git_ci") and not enf.get("sandbox"):
        return [_("enforce.git_ci needs enforce.sandbox: true")]
    if "git_push" in enf and (errs := validate_git_push(doc, enf)):
        return errs
    work = enf.get("work", False)
    if not isinstance(work, bool):
        return [_("enforce.work must be true or false")]
    # a discuss lease can write only because the OS sandbox blocks writes outside the folder
    if work and not enf.get("sandbox"):
        return [_("enforce.work needs enforce.sandbox: true")]
    caps = enf.get("capabilities")
    if caps is None:
        return []
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        return [_("enforce.capabilities must be a list of strings")]
    errs, have = [], capabilities()
    errs += [_("enforce.capabilities: the platform has no capability {cap} (it has: {have})",
               cap=c, have=", ".join(sorted(have)) or _("none")) for c in caps if c not in have]
    if caps and not enf.get("sandbox"):
        errs.append(_("enforce.capabilities needs enforce.sandbox: true"))
    # Not based on type: after an idea sandbox is upgraded into a real project, the capabilities the owner agreed to
    # stay with it (10-25 m10251626). The record of that consent is enforce itself: a platform-owned field that the
    # project's own manifest.update can't change (PLATFORM_OWNED); what a capability can do is decided by the
    # platform's Seatbelt profile, regardless of what kind of project it is
    return errs


GIT_PUSH_KEYS = {"branches", "emails", "public_emails", "max_file_mb", "visibility"}
GIT_PUSH_BRANCHES = ("none", "others", "all")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def origin_pushable(origin) -> bool:
    """paths.origin in a spelling brunna push contacts (git_remote.check_origin's syntax; where a local one is, that
    checks at push time)."""
    if not isinstance(origin, str) or not origin or origin == "(native)" or origin.startswith("-") \
            or re.match(r"^(ext|fd)::", origin) or not origin.isprintable():
        return False
    if "://" in origin:
        return not origin.lower().startswith("file://") or origin[len("file://"):].startswith("/")
    return bool(re.match(r"^[^/]+:", origin)) or origin.startswith(("/", "~/"))


def validate_git_push(doc: dict, enf: dict) -> list[str]:
    """enforce.git_push (docs/design/platform-push.md section 5), owner-only like the rest of enforce."""
    gp = enf.get("git_push")
    if not isinstance(gp, dict) or set(gp) - GIT_PUSH_KEYS:
        return [_("enforce.git_push can only be {{branches: none|others|all, emails: [..], public_emails: [..], "
                  "max_file_mb: 1-100, visibility: public|private}}")]
    if gp.get("branches", "none") not in GIT_PUSH_BRANCHES:
        return [_("enforce.git_push.branches must be none, others or all")]
    for key in ("emails", "public_emails"):
        v = gp.get(key, [])
        if not isinstance(v, list) or not all(isinstance(e, str) and e.isprintable() and EMAIL.fullmatch(e) for e in v):
            return [_("enforce.git_push.{key} must be a list of email addresses (local@domain, one line each)", key=key)]
    if set(gp.get("public_emails") or []) - set(gp.get("emails") or []):
        return [_("enforce.git_push.public_emails must also be in enforce.git_push.emails")]
    mb = gp.get("max_file_mb", 10)
    if not isinstance(mb, int) or isinstance(mb, bool) or not 1 <= mb <= 100:
        return [_("enforce.git_push.max_file_mb must be a whole number from 1 to 100")]
    if gp.get("visibility") not in (None, "public", "private"):
        return [_("enforce.git_push.visibility must be public or private")]
    if gp.get("branches", "none") == "none":
        return []
    if enf.get("sandbox") is not True:
        return [_("enforce.git_push needs enforce.sandbox: true (an unsandboxed project pushes with git as usual)")]
    if not origin_pushable((doc.get("paths") or {}).get("origin")):
        return [_("enforce.git_push needs a paths.origin brunna push contacts (a URL, host:path or an absolute path)")]
    if doc.get("name") in (hubconf.get("service_sandbox_exempt", []) or []):
        return [_("enforce.git_push can't be on for a project in service_sandbox_exempt: its jobs, the push's bundler "
                  "included, run with no sandbox")]
    return []


def validate_models(doc: dict) -> list[str]:
    """models: {<point>: {tier}}: the model calls this project makes through the platform (agenthub complete);
    metered_models: true = the owner allows metered (pay-per-use) providers for them (owner-only, PLATFORM_OWNED)."""
    errs = []
    models = doc.get("models")
    if models is not None:
        if not isinstance(models, dict):
            return [_("models must be a mapping of point name → {{tier: fast|standard|strong}}")]
        for name, decl in models.items():
            if not isinstance(name, str) or not MODEL_POINT.match(name):
                errs.append(_("models: point name {name!r} must be lowercase letters, digits and -", name=name))
            if not isinstance(decl, dict) or set(decl) - {"tier"} or decl.get("tier") not in TIERS:
                errs.append(_("models.{name} must be {{tier: fast|standard|strong}}", name=name))
    errs += validate_agents(doc)
    if "metered_models" in doc and not isinstance(doc["metered_models"], bool):
        errs.append(_("metered_models must be true or false"))
    if isinstance(doc.get("mailbox"), dict) and "wake" in doc["mailbox"] and not isinstance(doc["mailbox"]["wake"], bool):
        errs.append(_("mailbox.wake must be true or false"))
    return errs


def validate_agents(doc: dict) -> list[str]:
    """agents: {<name>: {tier, allow, timeout?}} and agents_per_day (docs/design/project-agent-calls.md A1): the shape
    only. What a run's rules may be is checked when it runs (A2), so an owner who tightens the project is never held
    back by them."""
    errs = []
    agents = doc.get("agents")
    if agents is not None:
        if not isinstance(agents, dict):
            return [_("agents must be a mapping of name → {{tier, allow, timeout?}}")]
        for name, decl in agents.items():
            if not isinstance(name, str) or not MODEL_POINT.match(name):
                errs.append(_("agents: name {name!r} must be lowercase letters, digits and -", name=name))
            if not isinstance(decl, dict) or set(decl) - {"tier", "allow", "timeout"}:
                errs.append(_("agents.{name} must be {{tier, allow, timeout?}}", name=name))
                continue
            if decl.get("tier") not in TIERS:
                errs.append(_("agents.{name}: tier must be one of {tiers}", name=name, tiers=", ".join(TIERS)))
            allow = decl.get("allow")
            if not isinstance(allow, list) or not allow or not all(rule_shaped(x) for x in allow):
                errs.append(_("agents.{name}: allow must be a non-empty list of permission rules, each a name (letters, "
                              "digits, _ or -), optionally its argument in parentheses, on one line", name=name))
            if "timeout" in decl and (not EVERY.match(str(decl["timeout"])) or every_minutes(str(decl["timeout"])) < 1):
                errs.append(_("agents.{name}: timeout must look like 45m / 1h", name=name))
    per_day = doc.get("agents_per_day")
    if per_day is not None and (not isinstance(per_day, int) or isinstance(per_day, bool) or per_day < 1):
        errs.append(_("agents_per_day must be a whole number of minutes, at least 1"))
    return errs


def agent_timeout_s(decl: dict) -> int:
    """A declared agent's timeout in seconds (default 30 minutes)."""
    return every_minutes(str(decl["timeout"])) * 60 if decl.get("timeout") else 1800


# paths too: a manifest.update setting paths.workspace to ~ would make the whole home folder its sandbox's to write
# (round 6, 11-01); onboarding, renaming and recovery write paths through their own scripts, not through a letter
PLATFORM_OWNED = ("type", "enforce", "network", "allow_write", "privacy", "metered_models", "paths")
# and one key inside a field a project may otherwise change: mailbox.wake: false turns off every wake of its sessions (a woken
# session keeps its permissions, so whether a letter may start a turn isn't the project's own letter to write)
PLATFORM_OWNED_NESTED = (("mailbox", "wake"),)


def connector_jobs(doc: dict) -> dict:
    """The jobs that use the owner's claude.ai connectors (connectors: true), whole, with the folder they run in: such
    a run logs in with the owner's subscription and loads the connectors its allow: names, so the project can't add
    one, or change one's rules, prompt or folder, by its own letter (owner-only, like PLATFORM_OWNED)."""
    return {j.get("name"): {**j, "cwd": j.get("cwd") or default_cwd(doc)}
            for j in doc.get("jobs") or [] if isinstance(j, dict) and j.get("connectors") is True}


def agent_state_name(project: str, job: dict) -> str:
    """The folder an agent job keeps its reports, status and (cwd: state) working directory in, under
    .state/agent-jobs/: its legacy_name, else <project>-<job>."""
    return job.get("legacy_name") or f"{project}-{job.get('name')}"


def state_name_clashes(index: dict) -> dict[str, list[str]]:
    """State folder names two agent jobs would share (project a's job b-c and project a-b's job c), or that are a mailbox
    handler's (mailbox-<project>, hubbus): {name: [jobs]}. A job could otherwise read and write another's folder, its
    reports and STOP file, by its own project's letter; none of them is scheduled."""
    by = {}
    for name, doc in [*index["projects"].items(), *(index.get("modules") or {}).items()]:
        for job in doc.get("jobs") or []:
            if isinstance(job, dict) and job.get("kind") == "agent":
                by.setdefault(agent_state_name(name, job), []).append(f"{name}/{job.get('name')}")
    return {n: js for n, js in by.items() if len(js) > 1 or n.startswith("mailbox-")}


def sandbox_enforced(doc: dict) -> bool:
    """The registry's enforce.sandbox, as render-project-settings decides it (not the services-only exemption)."""
    return (doc.get("enforce") or {}).get("sandbox") is True


def unconfined_sandboxed(index: dict) -> list[str]:
    """Agent jobs of a sandboxed project that declare no allow: (project/job). Such a job would run on judgment point
    agent-job, with the project's saved permissions and hooks, outside the confinement the project is held to; none of
    them is scheduled, and llm.py refuses one at run time too (docs/design/project-agent-calls.md A2)."""
    return [f"{name}/{job.get('name')}" for name, doc in sorted(index["projects"].items()) if sandbox_enforced(doc)
            for job in doc.get("jobs") or [] if isinstance(job, dict) and job.get("kind") == "agent" and not job.get("allow")]


def rules_refused(index: dict) -> dict[str, str]:
    """{project/job: why} for the confined agent jobs (allow:) whose rules their runs would refuse (the runtime's
    rule_check port; docs/design/project-agent-calls.md A2). Listed, never a validation error: an owner who tightens
    enforce.sandbox or network:, or changes runtimes, is never held back by a project's rules. A runtime without the port
    is said once: none of those runs happens there."""
    jobs = [(name, doc, job) for name, doc in sorted(index["projects"].items()) for job in doc.get("jobs") or []
            if isinstance(job, dict) and job.get("kind") == "agent" and job.get("allow")]
    agents = [(name, doc, agent, decl) for name, doc in sorted(index["projects"].items())
              for agent, decl in (doc.get("agents") or {}).items() if isinstance(decl, dict) and decl.get("allow")]
    if not jobs and not agents:
        return {}
    import runtime_ports
    try:
        rt = runtime_ports.load(None, ports=("rule_check",))
    except (runtime_ports.Unsupported, runtime_ports.MissingFunctions, LookupError, ImportError) as e:
        return {"*": _("the rules of confined agent jobs can't be checked on this runtime, so none of them runs: {why}",
                       why=e)}
    out = {}
    for name, doc, job in jobs:
        cwd = hubconf.STATE / "agent-jobs" / agent_state_name(name, job) if job.get("cwd") == "state" else None
        # an event job's prompt carries any bus writer's text (agent-job-event: prompt_source untrusted)
        problems = rt.rules_problems(job["allow"], sandboxed=sandbox_enforced(doc), network=list(doc.get("network") or []),
                                     untrusted="trigger" in job, cwd=cwd)
        if problems:
            out[f"{name}/{job.get('name')}"] = _("agent job {job} doesn't run until its allow: changes: {problems}",
                                                 job=f"{name}/{job.get('name')}", problems="; ".join(problems))
    for name, doc, agent, decl in agents:
        # a declared agent runs in the project's folder, loads no tool servers (project-agent), its prompt the project's
        problems = rt.rules_problems(decl["allow"], sandboxed=sandbox_enforced(doc), network=list(doc.get("network") or []),
                                     untrusted=False, servers=False)
        if problems:
            out[f"{name}/agents.{agent}"] = _("agent call {agent} doesn't run until its allow: changes: {problems}",
                                              agent=f"{name}/{agent}", problems="; ".join(problems))
    for job, why in credential_refusals(index).items():   # a credentials job: its rules against its servers too
        out[job] = f"{out[job]}; {why}" if job in out else why
    return out


def legacy_names(doc: dict) -> dict:
    """Each job's legacy_name: the name its state, reports and schedule state are kept under (the old schedule.yaml
    entry's). Set when a job moves into the registry, by the platform; a project's letter that sets or changes one could
    take over another job's state, so it is owner-only."""
    return {j.get("name"): j.get("legacy_name") for j in doc.get("jobs") or [] if isinstance(j, dict) and j.get("legacy_name")}


# a permission rule as a project writes it: a name (a tool, or a tool server's id with -), optionally one argument in
# parentheses, on one line; never an option. What a rule may say is the runtime adapter's, which refuses the same shape
RULE_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(\([^\n\r\0]*\))?")
LEGACY_NAME = re.compile(r"[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)?")


def rule_shaped(rule) -> bool:
    return isinstance(rule, str) and bool(RULE_SHAPE.fullmatch(rule))


JOB_PROMPT = re.compile(r"jobs/[A-Za-z0-9_-][A-Za-z0-9._-]*\.md")


def job_prompt(stem: str, job: dict) -> pathlib.Path | None:
    """An agent job's prompt: a file under jobs/ in the config's projects/<project>/, the owner's text (a project can't
    write the config repo). None when the registry entry names anything else (a path, .., another folder)."""
    rel = job.get("prompt") or f"jobs/{job.get('name')}.md"
    if not isinstance(rel, str) or not JOB_PROMPT.fullmatch(rel):
        return None
    home = (hubconf.registry_dir() / stem / "jobs").resolve()
    p = (hubconf.registry_dir() / stem / rel)
    return p if p.resolve().parent == home else None


def platform_owned_changes(new: dict, cur: dict) -> list[str]:
    """Security fields manifest.update can't change: list them when they differ from the current registry entry."""
    def nested(doc, top, key):
        return doc[top].get(key) if isinstance(doc.get(top), dict) else None

    changed = [k for k in PLATFORM_OWNED if new.get(k) != cur.get(k)]
    changed += [f"{top}.{key}" for top, key in PLATFORM_OWNED_NESTED if nested(new, top, key) != nested(cur, top, key)]
    if connector_jobs(new) != connector_jobs(cur):
        changed.append(_("a job with connectors: true"))
    if credential_entries(new) != credential_entries(cur):
        changed.append(_("a platform credential (secrets: with kind)"))
    if credential_jobs(new) != credential_jobs(cur):
        changed.append(_("a job with credentials:"))
    old = legacy_names(cur)
    if any(old.get(job) != name for job, name in legacy_names(new).items()):   # dropping one with its job is the project's
        changed.append(_("a job's legacy_name"))
    return [_("{key} is a security field that only brunna can change ({owner} confirms); this letter changes it", key=k,
              owner=hubconf.owner_tag()) for k in changed]


class LetterLoader(yaml.SafeLoader):
    """A manifest.update letter's YAML, without aliases: one that refers to itself (`a: &x {b: *x}`) would never end
    merging, and a chain of them (a billion laughs) would build without bound; nothing an entry says needs one."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.composer.ComposerError(None, None, _("aliases (*name) aren't accepted in a manifest.update"),
                                              self.peek_event().start_mark)
        return super().compose_node(parent, index)


def as_sent(new, patch):
    """What a part changed, as it now stands, in the part's own shape: the keys of a mapping it merged into, and no
    others (the reply shows the project its fields, never the owner's beside them)."""
    if not isinstance(patch, dict) or not isinstance(new, dict):
        return new
    return {k: as_sent(new[k], v) for k, v in patch.items() if k in new}


def merge_patch(cur, patch):
    """A part of an entry merged into it, as JSON Merge Patch (RFC 7386) does: a mapping merges key by key, null
    removes the key, anything else (a list included) replaces what was there."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(cur) if isinstance(cur, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def manifest_update(doc, sender: str, cur: dict | None, index: dict | None = None) -> tuple[dict | None, list[str]]:
    """What one manifest.update letter makes of the sender's entry: (the new entry, the problems). A letter with v is
    the whole entry and replaces it; one without is the part that changes, merged into the current entry
    (merge_patch(), onto its v2 form), so a project needn't read its entry to change one field. Either way the result
    is checked whole: it must validate, keep the sender's name, change no security field (platform_owned_changes) and add
    nothing only an entry from before may have (letter_errors, with the index the other entries are in)."""
    if not isinstance(doc, dict):
        return None, [_("not a YAML mapping")]
    if "v" in doc:
        new = doc
    elif cur is None:
        return None, [_("{sender} has no registry entry to change; a new entry is sent whole (with v)", sender=sender)]
    else:
        new = merge_patch(tiers.upgrade(cur), doc)
    errs = validate(new, sender)
    if new.get("name") != sender:
        errs.append(_("a project can only change its own registry entry (the sender is {sender}, the YAML's name is {name})",
                      sender=sender, name=new.get("name")))
    else:
        errs += platform_owned_changes(new, cur or {})
        if not errs:
            errs += letter_errors(new, cur, sender, index)
    return new, errs


def normalise(doc):
    """A registry document with its services in their one form (isolation.normal_service), as the index holds it."""
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), list):
        return doc
    return {**doc, "services": [isolation.normal_service(s) if isinstance(s, dict) else s for s in doc["services"]]}


# what each item of these lists must be: field -> whether it is needed (its readers: contracts-check, the agents that read
# what others publish)
LISTS = {"provides": {"contract": True, "schema": True}, "consumes": {"contract": True, "copy": True, "from": False},
         "publishes": {"kind": False, "path": False, "contract": False}}


def list_problems(doc: dict) -> list[str]:
    """provides, consumes, publishes and mailbox: each in the shape its readers read."""
    errs = [_("mailbox must be a mapping")] if not isinstance(doc.get("mailbox", {}), dict) else []
    for key, fields in LISTS.items():
        for i, item in enumerate(doc[key] if isinstance(doc.get(key), list) else []):
            bad = ["*"] if not isinstance(item, dict) else \
                [f for f, needed in fields.items() if (needed or f in item) and not (isinstance(item.get(f), str) and item[f]
                                                                                    and "\0" not in item[f])] + \
                [f for f in ("v",) if isinstance(item.get(f), (dict, list))]
            if bad:
                errs.append(_("{key}[{i}]: {fields} must be text", key=key, i=i, fields=", ".join(bad)))
    return errs


def readable(doc: dict) -> dict:
    """An entry accepted under older rules, without the parts its readers couldn't read."""
    out = {k: v for k, v in doc.items() if k != "mailbox" or isinstance(v, dict)}
    for key in LISTS:
        if key in out:
            items = out[key] if isinstance(out[key], list) else []
            out[key] = [x for x in items if not list_problems({key: [x]})]
    return normalise(out)


MAX_DEPTH = 40


def data_problems(doc, where: str = "", depth: int = 0) -> list[str]:
    """What of a document the index or a registry file can't hold as it is: a key that isn't text, a value that is no
    plain data, a number or a depth no reader takes, text with no UTF-8 form. (One entry that can't be written would
    otherwise stop the index for all.)"""
    if depth > MAX_DEPTH:
        return [_("{where}: nested too deep", where=where)]
    if isinstance(doc, dict):
        out = []
        for k, v in doc.items():
            if not isinstance(k, str) or data_problems(k):
                out.append(_("{where}: a key must be text, not {key!r}", where=where or "entry", key=k))
            else:
                out += data_problems(v, f"{where}.{k}" if where else k, depth + 1)
        return out
    if isinstance(doc, list):
        return [p for v in doc for p in data_problems(v, where, depth + 1)]
    if isinstance(doc, str):
        ok = not any(0xD800 <= ord(c) < 0xE000 for c in doc)
    elif isinstance(doc, float):
        ok = math.isfinite(doc)
    else:
        ok = doc is None or isinstance(doc, (bool, datetime.date)) or isinstance(doc, int) and abs(doc) < 2 ** 63
    return [] if ok else [_("{where}: a value the registry can't hold ({kind})", where=where, kind=type(doc).__name__)]


def written(doc) -> str:
    """A document as the index and its cache hold it. Raises where it can't be written."""
    text = json.dumps(doc, ensure_ascii=False, indent=1, default=str)
    text.encode()
    return text


def said(text: str, most: int = 20000) -> str:
    """Words an entry may have chosen, as one message carries them: no character that can't be written or passed on."""
    return "".join(c if c in "\n\t" or not isolation.has_control(c) else "\N{REPLACEMENT CHARACTER}" for c in str(text)[:most])


def validate(doc, stem: str) -> list[str]:
    """A registry document as it is read: v2, or v1 upgraded to v2 (tiers.upgrade; the one upgrade function, kept as
    long as any deployment may hold v1 files)."""
    if not isinstance(doc, dict):
        return [_("not a YAML mapping")]
    if (errs := data_problems(doc)):
        return errs
    if doc.get("v") not in (1, 2):
        return [_("v must be 1 or 2 (it is {v!r})", v=doc.get("v"))]
    return validate_v2(tiers.upgrade(doc), stem)


def validate_v2(doc, stem: str) -> list[str]:
    """Project v2 (the header's "schema v2")."""
    if not isinstance(doc, dict):
        return [_("not a YAML mapping")]
    doc = normalise(doc)
    errs = [] if doc.get("v") == 2 else [_("v must be 2 (it is {v!r})", v=doc.get("v"))]
    rt = doc.get("runtime")
    if isinstance(rt, dict) and set(rt) - {"deploy"}:
        errs.append(_("runtime keeps only deploy in project v2 (tests go in tests); not allowed: {keys}",
                      keys=", ".join(sorted(map(str, set(rt) - {"deploy"})))))
    return errs + _validate_body(doc, stem, TOP_V2, ("deploy",)) + validate_tests(doc, dirs(doc))


def _strings(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


def validate_tests(doc: dict, d: dict) -> list[str]:
    """v2's code, inputs, tests and commit_gate."""
    errs: list[str] = []
    if "code" in doc and not _strings(doc["code"]):
        errs.append(_("code must be a list of patterns"))
    if "commit_gate" in doc and not isinstance(doc["commit_gate"], bool):
        errs.append(_("commit_gate must be true or false"))
    inputs = doc.get("inputs", [])
    if not _strings(inputs):
        errs.append(_("inputs must be a list of kind:value strings"))
        inputs = []
    for item in inputs:
        kind, _sep, value = item.partition(":")
        if kind not in ("file", "config", "env", "cmd") or not value:
            errs.append(_("inputs: {item!r} must be file:<path>, config:<name>, env:<NAME> or cmd:<command>", item=item))
        elif kind == "file" and (value.startswith(("/", "~")) or ".." in pathlib.PurePosixPath(value).parts):
            errs.append(_("inputs: {item!r}: a file input is a path inside the repo", item=item))
        elif kind == "config" and ("/" in value or value in (".", "..")):
            errs.append(_("inputs: {item!r}: a config input is a bare file name", item=item))
        elif kind == "env" and not ENV_NAME.match(value):
            errs.append(_("inputs: {item!r}: an environment variable name is upper case letters, digits and _",
                          item=item))
        elif kind == "cmd" and (e := resolves(value, d["workspace"], outside=not runs_sandboxed(doc))):
            errs.append(_("inputs: {item!r}: {error}", item=item, error=e))
    tiers = doc.get("tests", [])
    if not isinstance(tiers, list):
        return errs + [_("tests must be a list")]
    seen: set[str] = set()
    for i, t in enumerate(tiers):
        if not isinstance(t, dict):
            errs.append(_("tests[{i}] must be a mapping", i=i))
            continue
        name = t.get("tier")
        where = name if isinstance(name, str) else f"tests[{i}]"
        errs += [_("tests.{tier}: unknown field {key}", tier=where, key=k) for k in t if k not in TIER_KEYS]
        if not isinstance(name, str) or not MODEL_POINT.match(name):
            errs.append(_("tests.{tier}: tier must be lowercase letters, digits and -", tier=where))
        elif name in seen:
            errs.append(_("tests.{tier}: the tier name is used twice", tier=where))
        seen.add(where)
        run = t.get("run")
        if not isinstance(run, str) or not run.strip():
            errs.append(_("tests.{tier}: run is required", tier=where))
        elif (e := resolves(run.replace("{jobs}", "1"), d["workspace"], outside=not runs_sandboxed(doc))):
            errs.append(_("tests.{tier}: run: {error}", tier=where, error=e))
        when = t.get("when")
        if not isinstance(when, list) or not when or any(w not in MOMENTS for w in when) or len(set(when)) != len(when):
            errs.append(_("tests.{tier}: when must list one or more of commit, merge, nightly, deploy", tier=where))
        elif "commit" in when and doc.get("commit_gate") is not True:
            errs.append(_("tests.{tier}: when: commit needs commit_gate: true", tier=where))
        if "paths" in t and not _strings(t["paths"]):
            errs.append(_("tests.{tier}: paths must be a list of patterns", tier=where))
        jobs = t.get("jobs")
        if jobs is not None:
            if not isinstance(jobs, dict) or set(jobs) - {"max", "env"}:
                errs.append(_("tests.{tier}: jobs can only be {{max, env}}", tier=where))
            else:
                mx, env = jobs.get("max"), jobs.get("env")
                if mx is not None and (isinstance(mx, bool) or not isinstance(mx, int) or mx < 1):
                    errs.append(_("tests.{tier}: jobs.max must be a whole number of at least 1", tier=where))
                if env is not None and (not isinstance(env, str) or not ENV_NAME.match(env)):
                    errs.append(_("tests.{tier}: jobs.env must be an environment variable name", tier=where))
                if mx is not None and env is None and "{jobs}" not in (run or ""):
                    errs.append(_("tests.{tier}: jobs.max needs {{jobs}} in run or jobs.env", tier=where))
    return errs


def _validate_body(doc: dict, stem: str, top: set[str], runtime_keys: tuple[str, ...]) -> list[str]:
    """What v1 and v2 share: everything but v and what tests are."""
    errs: list[str] = []
    if doc.get("name") != stem:
        errs.append(_("name ({name!r}) must be the same as the file name {stem}", name=doc.get("name"), stem=stem))
    errs += [_("unknown field {key}", key=k) for k in doc if k not in top]
    if doc.get("type", "project") not in ("project", "idea", "platform"):
        errs.append(_("type can only be project, idea or platform"))
    if doc.get("type") == "platform" and stem not in names.all("project"):
        errs.append(_("type: platform is the platform's own entry only"))
    if stem in names.all("project") and doc.get("type") != "platform" and stem != hubconf.hub_project():
        # both of the platform's names are kept for it (docs/design/rename-brunna.md): a project under either would
        # take letters addressed to the platform
        errs.append(_("{name} is reserved for the platform itself; pick another name", name=stem))
    if doc.get("purpose") is not None and (not isinstance(doc["purpose"], str) or not doc["purpose"].strip()):
        errs.append(_("purpose must be a non-empty string"))
    if doc.get("type") == "idea" and not doc.get("purpose"):
        errs.append(_("type: idea needs a purpose (the idea itself)"))
    is_hub = stem == hubconf.hub_project() or doc.get("type") == "platform"
    paths = doc.get("paths") or {}
    if not is_hub and not paths.get("workspace"):
        errs.append(_("paths.workspace is required"))
    errs += validate_enforce(doc)
    errs += validate_models(doc)
    li = doc.get("layout_intent")
    if li is not None and not (isinstance(li, dict) and (li.get("split") is True or isinstance(li.get("span"), list))):
        errs.append(_("layout_intent is split: true, or span: a list of projects (docs/design/newcomer-path.md B6)"))
    if doc.get("commits") not in (None, "conventional"):
        errs.append(_("commits can only be conventional (absent = commit titles are not checked)"))
    if doc.get("gate") not in (None, "strict", "detect"):
        errs.append(_("gate can only be strict or detect (the default)"))
    errs += validate_secrets(doc.get("secrets"))
    for key in ("network", "allow_write"):
        v = doc.get(key)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x for x in v)):
            errs.append(_("{key} must be a list of strings", key=key))
    if (doc.get("privacy") or {}).get("reflection", "include") not in ("include", "stats_only", "exclude"):
        errs.append(_("privacy.reflection can only be include / stats_only / exclude"))
    d = dirs(doc)
    rt = doc.get("runtime")
    if rt is not None:
        if not isinstance(rt, dict):
            errs.append(_("runtime must be a mapping"))
        elif d["runtime"] and not d["runtime"].is_dir():
            errs.append(_("the runtime directory {path} does not exist", path=d["runtime"]))
    for key in ("publishes", "provides", "consumes", "services", "jobs"):
        if not isinstance(doc.get(key, []), list):
            errs.append(_("{key} must be a list", key=key))
    errs += list_problems(doc)
    errs += validate_runs(doc, stem, d)
    for key in runtime_keys:
        if isinstance(rt, dict) and rt.get(key):
            base = d["workspace"] if key == "test" else d["workspace"]
            if (e := resolves(rt[key], base, outside=not runs_sandboxed(doc))):
                errs.append(_("runtime.{key}: {error}", key=key, error=e))
    return errs


def bus_event_types() -> list[str]:
    """The event types a job can react to: the type enum of the hubbus contract (one source of truth)."""
    schema = json.loads((hubconf.ROOT / "contracts" / "hubbus.v1.schema.json").read_text())
    return schema["properties"]["type"]["enum"]


def platform_doc(doc: dict, stem: str) -> bool:
    """The platform project and modules may react to other projects' events; an ordinary project only to its own."""
    return stem == hubconf.hub_project() or doc.get("type") in ("module", "platform")


def validate_trigger(job: dict, doc: dict, stem: str) -> list[str]:
    """A job's trigger: (docs/design/event-jobs.md section 2)."""
    on = job["trigger"]
    if not isinstance(on, dict):
        return [_("trigger must be a mapping")]
    errs = [_("trigger: unknown field {key}", key=k) for k in on if k not in TRIGGER]
    if "legacy_name" in job:
        errs.append(_("an event job (trigger:) cannot have legacy_name"))
    events = on.get("events")
    if not isinstance(events, list) or not events or not all(isinstance(x, str) and x for x in events):
        errs.append(_("trigger.events must be a non-empty list of bus event types"))
        events = []
    else:
        known = bus_event_types()
        errs += [_("trigger.events: {type} is not a bus event type (contracts/hubbus.v1.schema.json: {known})", type=t,
                   known=", ".join(known)) for t in events if t not in known]
        errs += [_("trigger.events: {type} is mailbox work (the mailbox delivers it); an event job can't react to it",
                   type=t) for t in events if t in MAILBOX_WORK]
    projects = on.get("projects", ["self"])
    if not isinstance(projects, list) or not projects or not all(isinstance(x, str) and x for x in projects):
        errs.append(_("trigger.projects must be a non-empty list (self, a project name or *)"))
    elif any(x not in ("self", stem) for x in projects) and not platform_doc(doc, stem):
        errs.append(_("trigger.projects: a project's job only sees its own project's events (self)"))
    # bus events are their writers' claims (any project can write the bus; event-jobs.md section 3.2)
    if job.get("kind") == "agent" and platform_doc(doc, stem):
        errs.append(_("the platform's event jobs can't be agent jobs: an event is what its writer claims, and an agent "
                      "with the platform's authority must not take instructions from it"))
    elif job.get("kind") == "agent":
        if job.get("connectors"):
            errs.append(_("an agent event job can't use connectors: (it would load the user's MCP servers)"))
        if not job.get("allow"):
            errs.append(_("an agent event job must declare allow: (anyone who can write the bus can start it with their "
                          "text; allow: is its boundary)"))
        if (job.get("cwd") or default_cwd(doc)) != "workspace":
            # the project's own protections (its sandbox, its rules) live in its workspace's settings: run anywhere
            # else, an agent anyone can start would run without them
            errs.append(_("an agent event job must run in the workspace (cwd: workspace), where the project's sandbox "
                          "and rules apply"))
    sources = on.get("sources")
    if sources is not None and (not isinstance(sources, list) or not sources
                                or not all(isinstance(x, str) and x for x in sources)):
        errs.append(_("trigger.sources must be a non-empty list of senders (globs allowed)"))
    where = on.get("where", {})
    if not isinstance(where, dict) or not all(isinstance(k, str) and k for k in where):
        errs.append(_("trigger.where must be a mapping from payload keys to values"))
        where = {}
    for k, v in where.items():
        vals = v if isinstance(v, list) else [v]
        if not vals or not all(isinstance(x, (str, int, float, bool)) for x in vals):
            errs.append(_("trigger.where.{key} must be a value or a non-empty list of values", key=k))
    if "message" in events:
        types = where.get("type")
        types = types if isinstance(types, list) else [types]
        if not types or not all(t in NOTICES for t in types):
            errs.append(_("trigger.events has message: limit it to notices with where.type ({types}); requests and questions "
                          "belong to the mailbox", types=", ".join(NOTICES)))
    deb = on.get("debounce", "0m")
    if str(deb) != "0m" and not EVERY.match(str(deb)):
        errs.append(_("trigger.debounce must look like 0m / 10m / 1h"))
    return errs


def service_fields(svc: dict, stem: str, doc: dict) -> list[str]:
    """One (normal) service's values: each as render-services writes it into a unit, or sandboxed-run into its sandbox."""
    errs = []
    if (why := isolation.service_problem(svc, allow="")):
        errs.append(why)
    if "label" in svc and not label_ok(svc["label"]):
        errs.append(_("label can only be letters, digits, . _ and -, starting with a letter, digit or _"))
    elif "label" in svc and not platform_doc(doc, stem) and _fold(svc["label"]) in core_labels():   # a sandboxed one's too, though it isn't used
        errs.append(_("the label {label} is the platform's own, or isn't a name", label=svc["label"]))
    elif not label_ok(label := service_label(stem, doc, svc)) or not platform_doc(doc, stem) and _fold(label) in core_labels():
        errs.append(_("the label {label} is the platform's own, or isn't a name", label=label))
    if not isinstance(svc.get("keepalive", False), bool):
        errs.append(_("keepalive must be true or false"))
    for key in ("interval_s", "throttle_s"):
        v = svc.get(key, 1)
        if not isinstance(v, int) or not 1 <= v <= YEAR_S:
            errs.append(_("{key} must be a number of seconds, at most {most}", key=key, most=YEAR_S))
    exp = svc.get("expose", {})
    if not isinstance(exp, dict):
        errs.append(_("expose must be a mapping (tailnet_https, local_port)"))
    else:
        errs += [_("expose: unknown field {key}", key=k) for k in exp if k not in ("tailnet_https", "local_port")]
        for key in ("local_port", "tailnet_https"):
            v = exp.get(key, 1)
            if not isinstance(v, int) or not 1 <= v <= 65535:
                errs.append(_("expose.{key} must be a port number", key=key))
    return errs


def in_a_cycle(jobs: list[dict], stem: str) -> list[str] | None:
    """A cycle of after:/before: among a project's own jobs (their names, in order), or None."""
    graph: dict[str, set[str]] = {j["name"]: set() for j in jobs if isinstance(j.get("name"), str)}

    def own(x: str) -> str | None:
        return x.removeprefix(f"{stem}/") if x.removeprefix(f"{stem}/") in graph else None
    for j in jobs:
        for key in ("after", "before"):
            for x in j.get(key) if isinstance(j.get(key), list) and j.get("name") in graph else []:
                if isinstance(x, str) and (o := own(x)):
                    graph[j["name"] if key == "after" else o].add(o if key == "after" else j["name"])
    found = find_cycles(graph)
    return found[0] if found else None


def validate_runs(doc: dict, stem: str, d: dict) -> list[str]:
    """services and jobs (shared by the project registry and modules)."""
    errs: list[str] = []
    names = set()
    sandboxed = runs_sandboxed(doc, stem)
    listed = {k: doc.get(k) if isinstance(doc.get(k), list) else [] for k in ("services", "jobs")}   # else said already
    services, jobs = ([x for x in listed[k] if isinstance(x, dict)] for k in ("services", "jobs"))
    if len(services) != len(listed["services"]):
        errs.append(_("each service must be a mapping"))
    if len(jobs) != len(listed["jobs"]):
        errs.append(_("each job must be a mapping"))
    taken: dict[str, str] = {}
    for svc in map(isolation.normal_service, services):
        n = svc.get("name")
        errs += [_("service {name}: unknown field {key}", name=n, key=k) for k in svc if k not in SERVICE]
        if not n or not svc.get("run"):
            errs.append(_("service {name}: name and run are required", name=n))
            continue
        if not isinstance(n, str) or not SERVICE_NAME.fullmatch(n) or ".." in n:
            errs.append(_("service {name!r}: name can only be letters, digits, . _ and -, starting with a letter, digit or _",
                          name=n))
            continue
        errs += [_("service {name}: {error}", name=n, error=e) for e in service_fields(svc, stem, doc)]
        if label_ok(label := service_label(stem, doc, svc)):
            if _fold(label) in taken:
                errs.append(_("service {name}: its label is already service {other}'s", name=n, other=taken[_fold(label)]))
            taken.setdefault(_fold(label), n)
        port = svc["expose"].get("local_port") if isinstance(svc.get("expose"), dict) else None
        if port in RESERVED_LOCAL_PORTS:
            errs.append(_("service {name}: local_port {port} is the platform's (the sign-in callback of brunna "
                          "credentials grant, or the status page)", name=n, port=port))
        cwd_key = svc.get("cwd") or default_cwd(doc)
        if cwd_key not in ("runtime", "workspace", "hub") or d.get(cwd_key) is None:
            errs.append(_("service {name}: cwd can only be runtime / workspace / hub, and one this project has", name=n))
            continue
        env = svc.get("env") if isinstance(svc.get("env"), dict) else {}
        own = env.get("PATH") if isinstance(env.get("PATH"), str) else None
        search = hubconf.expand_env(own or hubconf.LAUNCHD_PATH) if own or not sandboxed else None
        if isinstance(svc["run"], str) and (e := resolves(svc["run"], d[cwd_key], search, outside=not sandboxed)):
            errs.append(_("service {name}: {error}", name=n, error=e))
    for job in jobs:
        n = job.get("name")
        errs += [_("job {name}: unknown field {key}", name=n, key=k) for k in job if k not in JOB]
        if not isinstance(n, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", n):
            errs.append(_("job {name!r}: name must be lowercase letters, digits and -", name=n))
            continue
        if n in names:
            errs.append(_("job {name}: duplicate name", name=n))
        names.add(n)
        if sum(k in job for k in ("at", "every", "trigger")) != 1:
            errs.append(_("job {name}: give exactly one of at, every and trigger", name=n))
        if "trigger" in job:
            errs += [_("job {name}: {error}", name=n, error=e) for e in validate_trigger(job, doc, stem)]
        if "at" in job and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(job["at"])):
            errs.append(_("job {name}: at must be HH:MM (quoted)", name=n))
        if "every" in job and not EVERY.match(str(job["every"])):
            errs.append(_("job {name}: every must look like 15m / 1h", name=n))
        if "timeout" in job and not EVERY.match(str(job["timeout"])):
            errs.append(_("job {name}: timeout must look like 45m / 2h", name=n))
        if "catch_up" in job and (job["catch_up"] != "period" or "at" not in job):
            errs.append(_("job {name}: catch_up can only be period, on a job with at", name=n))
        errs += [_("job {name}: {error}", name=n, error=e) for e in job_when(job)]
        cwd_key = job.get("cwd") or default_cwd(doc)
        if cwd_key == "state" and job.get("kind") == "agent":
            # an agent job runs in its own state directory (without loading the project's settings/MCP; e.g. the
            # project's own MCP with the same name would shadow a claude.ai connector)
            pass
        elif cwd_key not in ("runtime", "workspace", "hub") or d.get(cwd_key) is None and not doc.get("type") == "module":
            errs.append(_("job {name}: cwd can only be runtime / workspace / hub (an agent job may also use state)", name=n))
            continue
        if job.get("allow") is not None and (not isinstance(job["allow"], list)
                                             or not all(isinstance(x, str) and x for x in job["allow"])):
            errs.append(_("job {name}: allow must be a list of permission rule strings", name=n))
        elif any(not rule_shaped(x) for x in job.get("allow") or []):
            # the project's own text, handed to the runtime as rules: never an option (the runtime's adapter refuses one too)
            errs.append(_("job {name}: each allow entry is one permission rule: a name (letters, digits, _ or -), optionally its argument in parentheses, on one line (never an option)", name=n))
        if job.get("legacy_name") is not None and not (isinstance(job["legacy_name"], str)
                                                       and LEGACY_NAME.fullmatch(job["legacy_name"])):
            # it names a state folder (run-agent-job.sh) and schedule state: a name, never a path
            errs.append(_("job {name}: legacy_name is a name (lowercase letters, digits, -; a module's may have one /)",
                          name=n))
        if "mail" in job and not isinstance(job["mail"], bool):
            errs.append(_("job {name}: mail must be true/false", name=n))
        elif job.get("mail") is True and (job.get("kind") != "agent" or not job.get("allow") or "trigger" in job):
            errs.append(_("job {name}: mail: true is only for a scheduled agent job with an allow: list (on an event job "
                          "any bus writer's text would send letters as the project)", name=n))
        if "tier" in job and (job.get("kind") != "agent" or not job.get("allow") or job["tier"] not in TIERS):
            errs.append(_("job {name}: tier is for an agent job with an allow: list (a run without one loads the "
                          "project's saved settings, which can change how it is paid for), one of {tiers}", name=n,
                          tiers=", ".join(TIERS)))
        if "connectors" in job and not isinstance(job["connectors"], bool):
            errs.append(_("job {name}: connectors must be true/false", name=n))
        if job.get("connectors") is True and not job.get("allow"):
            errs.append(_("job {name}: connectors: true needs an allow: list (its run denies every MCP server the list "
                          "doesn't name, so without one it could use no connector)", name=n))
        errs += [_("job {name}: {error}", name=n, error=e) for e in validate_job_credentials(job, doc)]
        if "found_exit" in job and (job["found_exit"] is not True or not platform_doc(doc, stem)
                                    or job.get("kind", "command") != "command" or "trigger" in job):
            errs.append(_("job {name}: found_exit: true is only for a scheduled command job of the platform or a module "
                          "(a check that sends what it finds itself; on a project's job it would hide its failures)",
                          name=n))
        if any(str(x).startswith("mcp__claude_ai_") for x in job.get("allow") or []) and not job.get("connectors"):
            errs.append(_("job {name}: allow has claude.ai connector tools; add connectors: true (the default headless token "
                           "loads no connectors)", name=n))
        if job.get("kind", "command") == "agent":
            prompt = job_prompt(stem, job)
            if prompt is None:
                errs.append(_("job {name}: prompt must be a file name under jobs/ in the config's projects/{project}/ (e.g. "
                              "jobs/{name}.md): the prompt is the owner's text, never a file the project writes",
                              name=n, project=stem))
            elif not prompt.exists():
                errs.append(_("job {name}: prompt {prompt} does not exist", name=n, prompt=prompt))
        elif job.get("kind", "command") != "command":
            errs.append(_("job {name}: kind can only be command / agent", name=n))
        elif not isinstance(job.get("run"), str) or not job["run"]:
            errs.append(_("job {name}: run is required", name=n))
        elif isolation.has_control(job["run"], allow="\t\n"):
            errs.append(_("job {name}: run holds a control character", name=n))
        elif (e := resolves(job["run"], d.get(cwd_key), outside=not sandboxed)):
            errs.append(_("job {name}: {error}", name=n, error=e))
    for job in jobs:
        for key in ("after", "before"):
            v = job.get(key)
            if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x for x in v)):
                errs.append(_("job {name}: {key} must be a list of job names", name=job.get("name"), key=key))
                continue
            for dep in v or []:
                if "/" not in dep and dep not in names:
                    errs.append(_("job {name}: {dep} in {key} is not in this project (another project or module is written "
                                   "<name>/<job>)", name=job.get("name"), key=key, dep=dep))
    if (cycle := in_a_cycle(jobs, stem)):
        errs.append(_("after:/before: form a cycle, these jobs would wait for each other forever: {cycle}",
                      cycle=" → ".join(cycle)))
    return errs


def job_when(job: dict) -> list[str]:
    """When a job runs, beyond at/every/trigger: each value in the form the scheduler reads."""
    errs = []
    if "days" in job and (not isinstance(job["days"], list) or not job["days"]
                          or not all(isinstance(x, str) and x in DAYS for x in job["days"])):
        errs.append(_("days can only be {days}", days=sorted(DAYS)))
    for key, most in (("days_of_month", 31), ("months", 12)):
        v = job.get(key, [1])
        if not isinstance(v, list) or not v or not all(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= most
                                                       for x in v):
            errs.append(_("{key} must be a list of numbers from 1 to {most}", key=key, most=most))
    if "until" in job:
        try:
            datetime.date.fromisoformat(str(job["until"]))
        except ValueError:
            errs.append(_("until must be a date (YYYY-MM-DD)"))
    if "between" in job and not (isinstance(job["between"], str) and BETWEEN.fullmatch(job["between"])):
        errs.append(_("between must look like 08:00-22:00"))
    n = job.get("max_attempts_per_day", 1)
    if not isinstance(n, (int, float)) or isinstance(n, bool) or not 0 <= n < 1e6:
        errs.append(_("max_attempts_per_day must be a number"))
    return errs


def foreign_deps(doc: dict, stem: str, key: str) -> set[tuple[str, str]]:
    """(job, dependency) where a job's after:/before: names another project's job."""
    return {(str(j.get("name")), x) for j in doc.get("jobs") or [] if isinstance(j, dict)
            for x in (j.get(key) if isinstance(j.get(key), list) else [])
            if isinstance(x, str) and "/" in x and x.split("/", 1)[0] != stem}


def ignored_before(index: dict) -> dict[str, str]:
    """{project/job: why} for a sandboxed project's jobs whose before: names another project's job: that part isn't used
    (it never decides when another's jobs run), the entry stays valid."""
    return {f"{name}/{job}": _("job {job}: before: {dep} is another project's job; it is ignored", job=f"{name}/{job}",
                               dep=dep)
            for name, doc in sorted(index["projects"].items()) if runs_sandboxed(doc, name)
            for job, dep in sorted(foreign_deps(doc, name, "before"))}


def letter_errors(new: dict, cur: dict | None, sender: str, index: dict | None) -> list[str]:
    """What a manifest.update may not add, though an entry that has it already keeps it: in a sandboxed project a job that
    is put before another project's or waits for one of an ordinary project's, and a service name with a dot; in any
    (with the index) a label another entry has."""
    errs, cur = [], cur or {}
    for key in ("after", "before") if runs_sandboxed(new, sender) else ():
        for job, dep in sorted(foreign_deps(new, sender, key) - foreign_deps(cur, sender, key)):
            owner = dep.split("/", 1)[0]
            if key == "after" and (owner not in ((index or {}).get("projects") or {})
                                   or platform_doc(index["projects"][owner], owner)):
                continue   # the platform's, a module's, or nobody's (which never waits)
            errs.append(_("job {name}: {key} may name only this project's own jobs (and after: the platform's): {dep}",
                          name=job, key=key, dep=dep))
    if runs_sandboxed(new, sender):
        had = {str(s.get("name")) for s in cur.get("services") or [] if isinstance(s, dict)}
        errs += [_("service {name}: a sandboxed project's new service is named with letters, digits, _ and -", name=s["name"])
                 for s in new.get("services") or [] if isinstance(s, dict) and str(s.get("name")) not in had
                 and not SANDBOXED_SERVICE_NAME.fullmatch(str(s.get("name")))]
    if index:
        held = {n: labels(n, d) for n, d in [*index["projects"].items(), *(index.get("modules") or {}).items()]}
        errs += label_clashes(sender, new, held, labels(sender, cur))
    return errs


def validate_job_credentials(job: dict, doc: dict) -> list[str]:
    """A job's credentials: (docs/design/platform-credentials.md C6), what the registry alone can tell; what its rules
    may be is the runtime's (credential_refusals)."""
    if "credentials" not in job:
        return []
    creds = job["credentials"]
    if not isinstance(creds, list) or not creds or not all(isinstance(c, str) and c for c in creds) \
            or len(set(creds)) != len(creds):
        return [_("credentials must be a list of the names of this project's platform credentials (secrets: with kind)")]
    errs = []
    if job.get("kind") != "agent" or not job.get("allow") or "trigger" in job:
        errs.append(_("credentials: is only for a scheduled agent job with an allow: list (an event job's prompt is any "
                      "bus writer's text)"))
    if job.get("connectors"):
        errs.append(_("credentials: and connectors: true don't go together (a credentials job runs sealed, on the "
                      "platform's login)"))
    if sandbox_enforced(doc):
        errs.append(_("credentials: is not for a sandboxed project yet: its confined runs may name no tool server's tools "
                      "(docs/design/platform-credentials.md C7)"))
    if doc.get("type") == "module":
        errs.append(_("credentials: is for a project's jobs, not a module's"))
    if doc.get("type") == "platform" or doc.get("name") in names.all("project"):
        # its rules may write the platform's own state, where a run's bridge keeps its status
        errs.append(_("credentials: is not for the platform's own jobs (their rules may write the platform's state)"))
    have = credential_entries(doc)
    errs += [_("{name} is not a platform credential of this project (secrets: with kind)", name=c) for c in creds
             if c not in have]
    return errs


def credential_refusals(index: dict, rt=None) -> dict[str, str]:
    """{project/job: why} for the credentials jobs whose rules don't fit what such a run may have (the runtime's
    credential_tools port: the rules name exactly the tools of the servers it lists, and nothing that runs code or reads
    outside its folders). Listed, as rules_refused is; llm.py refuses the run too."""
    jobs = [(name, doc, job) for name, doc in sorted(index["projects"].items()) for job in doc.get("jobs") or []
            if isinstance(job, dict) and job.get("credentials") and job.get("allow")]
    if not jobs:
        return {}
    import runtime_ports
    if rt is None:
        try:
            rt = runtime_ports.load(None, ports=("credential_tools",))
        except (runtime_ports.Unsupported, runtime_ports.MissingFunctions, LookupError, ImportError) as e:
            return {f"{n}/{j.get('name')}": _("its credentials can't be given on this runtime: {why}", why=e)
                    for n, _d, j in jobs}
    out = {}
    for name, doc, job in jobs:
        have = credential_entries(doc)
        servers = {have[c]["server"]["id"] for c in job["credentials"] if c in have}
        problems = rt.credential_rules_problems(job["allow"], servers)
        if problems:
            out[f"{name}/{job.get('name')}"] = _("agent job {job} doesn't run until its allow: changes: {problems}",
                                                 job=f"{name}/{job.get('name')}", problems="; ".join(problems))
    return out


# ---------- modules (docs/design/new-user.md 3.2) ----------
# Optional features (personal assistant, backup, mirror, weekly reflection…): modules/<name>.yaml in the platform
# repo, in the same format as the project registry's jobs/services (cwd is always hub); used only with
# modules.<name>: true in hub.json. In the schedule a job is called <module>/<job>.
# A job moved into a module carries its old full name in legacy_name (e.g. agent-hub/backup), so its schedule state
# moves over and others' after: still match.

MODULES_DIR = hubconf.ROOT / "modules"
MODULE_TOP = {"v", "name", "type", "description", "requires_config", "needs_llm", "jobs", "services", "secrets",
              "runtime_preset",
              "consumes"}


def validate_module(doc, stem: str) -> list[str]:
    if not isinstance(doc, dict):
        return [_("not a YAML mapping")]
    errs = [_("unknown field {key}", key=k) for k in doc if k not in MODULE_TOP]
    rp = doc.get("runtime_preset")
    if rp is not None:
        if (not isinstance(rp, dict) or not isinstance(rp.get("label"), str) or not isinstance(rp.get("find"), str)
                or not isinstance((rp.get("settings") or {}).get("runtime"), dict)
                or not all(isinstance(x, str) for x in rp.get("install") or [])
                or not all(isinstance(x, str) for x in rp.get("mcp_register") or [])
                or not all(isinstance(x, str) for x in rp.get("mcp_unregister") or [])):
            errs.append(_("runtime_preset needs label, find, settings.runtime and install as a list of lines "
                          "(docs/design/newcomer-path.md B4)"))
        elif "login_use" in rp and not (isinstance(rp["login_use"], str) and rp["login_use"].strip()):
            errs.append(_("runtime_preset.login_use is the text asked of the owner"))
    if doc.get("v") != 1:
        errs.append(_("v must be 1"))
    if doc.get("name") != stem:
        errs.append(_("name ({name!r}) must be the same as the file name {stem}", name=doc.get("name"), stem=stem))
    if doc.get("type") != "module":
        errs.append(_("type must be module"))
    if not isinstance(doc.get("description"), str) or not doc["description"].strip():
        errs.append(_("description is required"))
    req = doc.get("requires_config") or []
    if not isinstance(req, list):
        errs.append(_("requires_config must be a list of {settings} keys"))
    else:
        errs += [_("{key} must be set in {settings} first", key=k) for k in req if hubconf.get(k) in (None, "", {}, [])]
    # needs_llm: true = the module's work is judgment points, so it does nothing useful without a model for them
    # (agenthub init leaves it off then; agenthub modules says so)
    if not isinstance(doc.get("needs_llm", False), bool):
        errs.append(_("needs_llm must be true or false"))
    for key in ("jobs", "services"):
        for x in doc.get(key) or []:
            if isinstance(x, dict) and x.get("cwd", "hub") != "hub":
                errs.append(_("{kind} {name}: a module's cwd can only be hub", kind=key[:-1], name=x.get("name")))
            if key == "jobs" and isinstance(x, dict) and x.get("kind", "command") != "command":
                errs.append(_("job {name}: a module can only have command jobs", name=x.get("name")))
    # the module's own credentials (the platform's; modules that are off aren't checked)
    errs += validate_secrets(doc.get("secrets"), credentials_ok=False)
    # contracts of others the module reads (e.g. the assistant module reads the assistant's handoff format); modules
    # that are off aren't compared
    for c in doc.get("consumes") or []:
        if not isinstance(c, dict) or not all(c.get(k) for k in ("contract", "v", "copy")):
            errs.append(_("each consumes item needs contract, v and copy"))
    return errs + validate_runs(doc, stem, dirs(doc))


def enabled_modules() -> list[str]:
    m = hubconf.get("modules") or {}
    return sorted(k for k, v in m.items() if not k.startswith("_") and v is True)


def load_modules() -> tuple[dict, dict]:
    """(enabled module → doc, errors). Enabled but no file, or invalid: record the error and don't use it (its
    jobs/services then don't exist)."""
    mods, errors = {}, {}
    for name in enabled_modules():
        f = MODULES_DIR / f"{name}.yaml"
        if not f.exists():
            errors[f"module:{name}"] = [_("{settings} enables module {name}, but there is no modules/{name}.yaml", name=name)]
            continue
        try:
            doc = yaml.safe_load(f.read_text())
            errs = validate_module(doc, name)
        except yaml.YAMLError as e:
            doc, errs = None, [_("cannot read the YAML: {error}", error=e)]
        doc = normalise(doc)
        if errs:
            errors[f"module:{name}"] = errs
        else:
            mods[name] = doc
    return mods, errors


def job_cycles(index: dict) -> list[str]:
    """Whether after:/before: chain into a cycle (if so the scheduler would wait forever)."""
    jobs, _skipped, _renames = scheduler_jobs(index, set())
    graph = {j["name"]: set(j.get("after") or []) for j in jobs}
    return [" → ".join(c) for c in find_cycles(graph)]


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Cycles of a graph (each node's edges), each as its nodes with the first repeated at the end. Not recursive: a chain
    of any length is read."""
    out, state = [], {}
    for start in sorted(graph):
        if state.get(start):
            continue
        state[start], path, todo = 1, [start], [iter(sorted(graph[start]))]
        while todo:
            dep = next(todo[-1], None)
            if dep is None:
                todo.pop()
                state[path.pop()] = 2
            elif dep in graph and state.get(dep) == 1:
                out.append([*path[path.index(dep):], dep])
            elif dep in graph and not state.get(dep):
                state[dep] = 1
                path.append(dep)
                todo.append(iter(sorted(graph[dep])))
    return out


def roster_mismatch(doc, text: str) -> list[str]:
    """The paths hubconf.roster() reads (a line-by-line parse without PyYAML, used by recover, sync-all-memory and
    the roster) must match the YAML, or those scripts silently get empty or wrong paths."""
    if not isinstance(doc, dict) or not isinstance(doc.get("paths") or {}, dict):
        return []
    want = {k: (None if v is None else str(v)) for k, v in (doc.get("paths") or {}).items()}
    got = hubconf.roster_paths(text)
    if got == want:
        return []
    return [_("paths must be written in block format, one key per line (`paths:`, then `workspace: …` indented on the "
              "next line): the reader that does not use YAML got {got}, YAML has {want}", got=got, want=want)]


def wrong_registry(old: dict, reg: pathlib.Path, files: list[pathlib.Path]) -> str | None:
    """Why this process must not rebuild the index (None = it may). The index lives in STATE, which follows the hub root,
    not the config: a process with another config (a test's, an empty HOME's) that reaches the real STATE would
    otherwise rebuild it from its own registry and drop every project (10-30, between 11:49 and 11:53, nobody could
    send). So an index that has projects is only rebuilt from the registry dir it was built from, and never from an
    empty one; `registry.py check --rebuild` is for when that really changed."""
    # the platform's own entry isn't a user project, under either name (an index from before the rename lists it as
    # agent-hub, one from before type: platform without the type)
    users = [n for n, d in (old.get("projects") or {}).items()
             if n not in names.all("project") and (d or {}).get("type") != "platform"]
    if not users:
        return None
    if old.get("registry_dir") not in (None, str(reg.resolve())):
        return _("the registry index was built from {old} and this process sees {dir}; not rebuilt (another config "
                 "reaching this hub's state?). If the registry really moved: registry.py check --rebuild",
                 old=old["registry_dir"], dir=reg)
    if not [f for f in files if f.stem not in names.all("project")]:
        return _("{dir} has no project files, but the registry index lists {n} {n:project|projects}; not rebuilt. If they really "
                 "were all removed: registry.py check --rebuild", dir=reg, n=len(users))
    return None


REFUSED_LOG = hubconf.STATE / "registry-refused.jsonl"


def record_refusal(why: str, reg: pathlib.Path) -> str:
    """Who tried to rebuild the index from the wrong registry: appended to REFUSED_LOG, so the next attempt names the
    process instead of us replaying test runs. Returns a one-line summary for the '(registry)' error."""
    ppid = os.getppid()
    try:
        parent = subprocess.run(["ps", "-o", "command=", "-p", str(ppid)], capture_output=True, text=True,
                                timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):   # a tool's sandbox may forbid ps
        parent = None
    entry = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "why": why,
             "argv": sys.argv, "pid": os.getpid(), "ppid": ppid, "parent": parent, "cwd": os.getcwd(),
             "registry_dir": str(reg), "config": str(hubconf.config_dir()), "home": os.environ.get("HOME"),
             "state": str(hubconf.STATE)}
    try:
        with REFUSED_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return f"at {entry['at']} by pid {entry['pid']} (parent {ppid}: {(parent or '?')[:200]}), argv {sys.argv}, " \
           f"cwd {entry['cwd']}, config {entry['config']}; log {REFUSED_LOG}"


_said_superseded = False


def superseded() -> bool:
    """The checkout has moved on from the code this process started with (hubconf.LOADED_REV, noted when the process
    imported hubconf, its first platform import: a module imported lazily after a merge is new beside old ones): said
    once on stderr. refresh() writes the live index only while this is False (docs/design/service-code.md D2), so a
    process started before a merge (a long-running service, a long job, a command caught mid-merge) never overwrites what
    the new code wrote. None (not a git checkout): nothing to compare."""
    global _said_superseded
    LOADED_REV = getattr(hubconf, "LOADED_REV", None)   # an old hubconf in memory (a service started before it) has none
    if LOADED_REV is None:
        return False
    try:
        now = hubconf.code_rev()
    except OSError:
        now = None
    if now is None or now == LOADED_REV:
        return False
    if not _said_superseded:
        _said_superseded = True
        print("registry: " + _("this process runs the code of {old}, the checkout is at {new}: the live index is left to "
                               "the new code", old=LOADED_REV[:8], new=now[:8]), file=sys.stderr)
    return True


def refresh(only: list[str] | None = None, rebuild: bool = False) -> dict:
    """Validate the whole registry, refresh the cache and index.json, return the index. From a linked worktree of the
    hub that found the live state through the main checkout (hubconf.worktree_lookup), or in a process whose code the
    checkout has moved on from (superseded, docs/design/service-code.md D2), it validates and returns the index but
    writes nothing: the live index is built only by the main checkout's current code."""
    live = not hubconf.worktree_lookup() and not superseded()
    if live:
        if CACHE == hubconf.REGISTRY_INDEX.parent:   # the live cache: the one in .state moved over once (11-08)
            hubconf.moved_from_state(CACHE)
        CACHE.mkdir(parents=True, exist_ok=True)
    reg = hubconf.registry_dir()
    files = sorted(reg.glob("*.yaml")) if reg.is_dir() else []
    try:
        old = json.loads((CACHE / "index.json").read_text())
    except (OSError, ValueError):
        old = {}
    if live and not rebuild and (old.get("format") or 1) > INDEX_FORMAT:
        # an index built by newer code (a long-running process with old code refreshing on a request): left alone,
        # said loudly; after a revert, `registry.py check --rebuild` is the owner's way back
        why = _("the index was built by newer code (format {have}, this code writes {mine}); not overwritten",
                have=old.get("format"), mine=INDEX_FORMAT)
        print(f"registry: {why}", file=sys.stderr)
        return {**old, "errors": {**(old.get("errors") or {}), "(registry)": [why]}}
    if not rebuild and (why := wrong_registry(old, reg, files)):
        who = record_refusal(why, reg) if live else ""
        print(f"registry: {why}\n  {who}", file=sys.stderr)
        return {**old, "errors": {**(old.get("errors") or {}), "(registry)": [why, who]}}
    # The platform's own registry entry is read wherever registry_dir is, and always from the platform repo: a file
    # with the same name in the user's registry (e.g. an extra copy made during the move) isn't used, or the
    # platform's updates to its own entry would never take effect
    plat = hubconf.platform_registry()
    if plat.exists():
        files = sorted([f for f in files if f.stem != plat.stem] + [plat])
    projects, errors = {}, {}
    read: dict[str, dict] = {}      # the valid ones, as read
    last: dict[str, dict] = {}      # each one's last valid version
    for f in files:
        stem = f.stem
        try:   # one entry, whatever is wrong with it, never stops the others
            text = f.read_text()
            doc = yaml.safe_load(text)
            errs = validate(doc, stem) + roster_mismatch(doc, text)
            doc = normalise(tiers.upgrade(doc))   # what the index holds is v2, whatever the file says
            if not errs:
                written(doc)
        except yaml.YAMLError as e:
            doc, errs = None, [_("cannot read the YAML: {error}", error=e)]
        except Exception as e:   # noqa: BLE001
            doc, errs = None, [_("the entry can't be checked: {error}", error=f"{type(e).__name__}: {e}"[:200])]
        try:
            last[stem] = readable(tiers.upgrade(json.loads((CACHE / f"{stem}.json").read_text())))
            written(last[stem])
        except Exception:   # noqa: BLE001 (none, or one that can't be held: its entry is then only what its file says)
            last.pop(stem, None)
        if errs:
            errors[stem] = errs
        else:
            read[stem] = doc
    modules, mod_errors = load_modules()
    # a label two entries take: the one that takes it anew is the newcomer, and isn't accepted
    held = {n: labels(n, d) for n, d in modules.items()}
    for stem, doc in last.items():
        mine = labels(stem, doc)
        held[stem] = {k: v for k, v in mine.items() if k in labels(stem, read.get(stem, doc))}
    for stem in [s for s in read if not platform_doc(read[s], s)]:
        if (clash := label_clashes(stem, read[stem], held, labels(stem, last.get(stem)))):
            errors[stem] = clash
            del read[stem]
    for stem in [f.stem for f in files]:
        if stem in read:
            projects[stem] = read[stem]
            if live and not superseded():
                (CACHE / f"{stem}.json").write_text(written(read[stem]))
        elif stem in last:   # keep using the last valid version (upgraded too: it may predate v2)
            projects[stem] = last[stem]
    # projects whose registry file was deleted: drop the cache too (deleting the entry = removing the project)
    for c in CACHE.glob("*.json") if live and not superseded() else []:
        if c.name != "index.json" and c.stem not in {f.stem for f in files}:
            c.unlink()
    errors.update(mod_errors)
    index = {"v": 1, "format": INDEX_FORMAT, "generated": datetime.datetime.now().isoformat(timespec="seconds"), "registry_dir": str(reg.resolve()),
             "projects": projects, "modules": modules, "errors": errors}
    index["refused"] = {}   # filled below; scheduler_jobs adds what it can't read
    if (cycles := job_cycles(index)):
        errors["(jobs)"] = [_("after:/before: form a cycle, these jobs would wait for each other forever: {cycle}", cycle=c)
                            for c in cycles]
    if (clash := state_name_clashes(index)):
        errors.setdefault("(jobs)", []).extend(
            _("agent jobs {jobs} would share the state folder {name} (or take a mailbox handler's): none of them runs "
              "until one is renamed", jobs=", ".join(sorted(js)), name=n) for n, js in sorted(clash.items()))
    # not an error: the entry stays valid and the config can still be committed and applied (an owner who turns a project's
    # sandbox on is never held back by its jobs, nor can a project hold back the config); the job alone doesn't run, and
    # `registry.py check` and the status page say why (docs/design/project-agent-calls.md A2)
    index["refused"] |= ignored_before(index)
    index["refused"] |= {
        j: _("agent job {job} belongs to a sandboxed project and declares no allow list, so it doesn't run until it "
             "declares the tools it needs (an agent job without one loads the project's saved permissions)", job=j)
        for j in unconfined_sandboxed(index)}
    if live and not superseded():   # asked again: a merge may have landed while this one validated
        tmp = CACHE / "index.json.tmp"
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1, default=str))
        tmp.replace(CACHE / "index.json")
    return index


def every_minutes(s: str) -> int:
    n, unit = EVERY.match(s).groups()
    return int(n) * (60 if unit == "h" else 1)


NIGHTLY_AT = "04:00"


def nightly_test_jobs(index: dict, taken: set[str]) -> list[dict]:
    """The nightly run of every project that declares a tier for it (docs/design/test-waiting.md B4): synthesized here,
    not written into the project's file, so a manifest.update cannot remove the run that overrides flaky passes.
    `agenthub test --when nightly --force --rev HEAD` in the project's main checkout, as one of its command jobs
    (sandboxed like the others when the project is)."""
    out = []
    # the platform tests itself only where its owner said so (tests.platform_nightly): its suite takes every core for
    # minutes and, on macOS, makes and attaches disk images. Never inferred: a checkout on main is not a developer's (11-25)
    own = hubconf.get("tests.platform_nightly") is True
    for name, doc in sorted(index["projects"].items()):
        if not any("nightly" in (t.get("when") or []) for t in doc.get("tests") or []):
            continue
        if name == hubconf.hub_project() and not own:
            continue
        full = f"{name}/nightly-tests"
        cwd = dirs(doc)["workspace"]
        if full in taken or not cwd:
            continue
        argv = [*hubconf.launch(hubconf.ROOT / "bin" / "test-runner.py"), "--when", "nightly", "--force", "--rev", "HEAD"]
        cmd = (shlex.join(isolation.wrap(name, cwd, argv)) if isolation.sandboxed(doc)
               else f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}")
        out.append({"name": full, "command": cmd, "project": name, "time": NIGHTLY_AT, "background": True,
                    "timeout_minutes": 180, "max_attempts_per_day": 1})
    return out


def scheduler_jobs(index: dict, legacy_names: set[str]) -> tuple[list[dict], list[str], dict[str, str]]:
    """Registry → scheduler.py's job dicts. Returns (jobs, the ones not used because their old name is still in
    schedule.yaml, new name → old name)."""
    out, skipped, renames = [], [], {}
    clash = state_name_clashes(index)
    loose = set(unconfined_sandboxed(index))
    before: dict[str, list[str]] = {}
    docs = sorted(index["projects"].items()) + sorted((index.get("modules") or {}).items())

    def add(name: str, doc: dict, d: dict, job: dict) -> None:
        full = f"{name}/{job['name']}"
        if job.get("legacy_name") in legacy_names:
            skipped.append(full)
            return
        if job.get("legacy_name"):
            renames[full] = job["legacy_name"]
        cwd = d.get(job.get("cwd") or default_cwd(doc))
        if job.get("kind") == "agent" and job.get("cwd") == "state":
            cwd = hubconf.STATE / "agent-jobs" / agent_state_name(name, job)
        if job.get("kind") == "agent":
            prompt = job_prompt(name, job)
            if prompt is None or agent_state_name(name, job) in clash or full in loose:   # refused (refresh says why)
                return
            agent_name = agent_state_name(name, job)
            # every agent job names its project, so the run can check what the project is held to (llm.py)
            cmd = (f"AGENT_JOB_PROJECT={shlex.quote(name)} "
                   f"{shlex.quote(str(hubconf.ROOT / 'bin' / 'run-agent-job.sh'))} {shlex.quote(agent_name)} "
                   f"{shlex.quote(str(cwd))} {shlex.quote(str(prompt))}")
            # unattended permissions: only these rules and the platform's settings for the project (run-agent-job.sh
            # runs it under judgment point agent-job-confined; docs/design/event-jobs.md 3.2)
            if job.get("allow"):
                cmd = f"AGENT_JOB_ALLOW={shlex.quote(json.dumps(job['allow'], ensure_ascii=False))} {cmd}"
            # the platform's mail tools, only for a job that asks (llm-points mail_from_env)
            if job.get("mail") is True:
                cmd = f"AGENT_JOB_MAIL=1 {cmd}"
            # the platform credentials it gets (llm-points credentials_from_env; llm.py reads them from the
            # registry by this job's name, never from the environment)
            if job.get("credentials"):
                cmd = f"AGENT_JOB_CREDENTIALS={shlex.quote(job['name'])} {cmd}"
            # the model strength it chose (llm-points tier_from_env), behind llm.py's billing gate
            if job.get("tier"):
                cmd = f"AGENT_JOB_TIER={shlex.quote(job['tier'])} {cmd}"
            # needs claude.ai connectors: use the subscription login, not the default headless token
            if job.get("connectors"):
                cmd = f"LLM_AUTH=subscription {cmd}"
        elif isolation.sandboxed(doc):
            # a sandboxed project's command job runs under its profile (bin/sandboxed-run; refuses where it can't)
            cmd = shlex.join(isolation.wrap(name, cwd, ["/bin/sh", "-c", job["run"]]))
        else:
            cmd = f"cd {shlex.quote(str(cwd))} && {job['run']}"
        j = {"name": full, "command": cmd, "project": name}
        if job.get("kind") == "agent":
            j["agent_name"] = agent_name
        if "at" in job:
            j["time"] = str(job["at"])
        elif "every" in job:
            j["every_minutes"] = every_minutes(str(job["every"]))
        for src, dst in (("days", "days_of_week"), ("days_of_month", "days_of_month"), ("months", "months"),
                         ("until", "end_date"), ("between", "between"), ("background", "background"),
                         ("max_attempts_per_day", "max_attempts_per_day"), ("catch_up", "catch_up"),
                         ("found_exit", "found_exit")):
            if src in job and (src != "found_exit" or platform_doc(doc, name)):
                j[dst] = job[src]
        if "trigger" in job:
            on = job["trigger"]
            deb = str(on.get("debounce", "0m"))
            # self = this project under its name and the names it had before a rename (old events keep those)
            own = [name, *[o for o in hubconf.former_names(name) if o not in index["projects"]]]
            projects = [x for p in on.get("projects", ["self"]) for x in (own if p in ("self", name) else [p])]
            j["trigger"] = {"events": list(on["events"]), "sources": list(on.get("sources") or []),
                       "projects": list(dict.fromkeys(projects)),
                       "where": dict(on.get("where") or {}),
                       "debounce_minutes": 0 if deb == "0m" else every_minutes(deb)}
            j.setdefault("max_attempts_per_day", event_jobs.DEFAULT_CAP)
            j["platform"] = platform_doc(doc, name)
            if job.get("kind") == "agent":
                # the scheduler owns the detachment so it sees the run's real outcome (event-jobs.md section 2);
                # judgment point agent-job-event: only allow: and the platform's settings apply (section 3.2)
                j["command"] = f"RUN_AGENT_JOB_FOREGROUND=1 AGENT_JOB_POINT=agent-job-event {j['command']}"
                j["background"] = True
        if "timeout" in job:
            j["timeout_minutes"] = every_minutes(str(job["timeout"]))
        if job.get("after"):
            j["after"] = [a if "/" in a else f"{name}/{a}" for a in job["after"]]
        for t in job.get("before") or []:
            if "/" in t and t.split("/", 1)[0] != name and runs_sandboxed(doc, name):
                continue   # another project's job: ignored, and said (ignored_before)
            before.setdefault(t if "/" in t else f"{name}/{t}", []).append(full)
        out.append(j)

    for name, doc in docs:
        for job in doc.get("jobs") or []:
            try:   # one job that can't be read never stops the others
                add(name, doc, dirs(doc), job)
            except Exception as e:   # noqa: BLE001
                full = f"{name}/{job.get('name') if isinstance(job, dict) else '?'}"
                index.setdefault("refused", {})[full] = _("job {job} doesn't run, its entry can't be read: {error}", job=full,
                                                          error=f"{type(e).__name__}: {e}"[:200])
                index.setdefault("unreadable", {})[full] = type(e).__name__   # the scheduler tells, in no words of the entry's
    out += nightly_test_jobs(index, {j["name"] for j in out})
    # before: [X] = X's after gets one more entry, me (a module inserts itself into the core flow without the core
    # knowing about it). If X doesn't exist (that module is off), ignore it
    by_name = {j["name"]: j for j in out}
    for target, deps in before.items():
        if target in by_name:
            by_name[target]["after"] = list(dict.fromkeys((by_name[target].get("after") or []) + deps))
    return out, skipped, renames


def tilde(p: str) -> str:
    home = str(pathlib.Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def new_doc(name: str, workspace: str, origin: str, memory_bare: str) -> dict:
    """The default registry entry at onboarding."""
    return {"v": 2, "name": name,
            "paths": {"workspace": tilde(workspace), "memory_bare": tilde(memory_bare), "origin": origin},
            "memory": True, "privacy": {"reflection": "include"}, "push_on_merge": False,
            "mailbox": {"escalate": True},
            "publishes": [{"kind": "state", "path": "STATE.md", "contract": "state-todo", "v": 2}]}


def header() -> str:
    return "# " + _("Project registry (schema v2, see bin/registry.py)") + "\n"


def cmd_add(name: str, workspace: str, origin: str, memory_bare: str, extra: str | None = None) -> int:
    """extra: a JSON object overriding fields of the default entry (e.g. an idea sandbox's
    type/purpose/enforce/privacy). Nothing is written if validation fails."""
    f = hubconf.registry_dir() / f"{name}.yaml"
    if f.exists():
        print(_("{name} is already in the registry ({file}); not overwriting", name=name, file=f))
        return 0
    doc = new_doc(name, workspace, origin, memory_bare)
    if extra:
        doc.update(json.loads(extra))
        errs = validate(doc, name)
        if errs:
            print(_("the registry entry is invalid; not written: {errors}", errors="; ".join(errs)), file=sys.stderr)
            return 1
    hubconf.write_atomic(f, header() + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200), keep=True)
    print(_("registry: {file}", file=f))
    # known at once, not at the scheduler's next tick (which may not run yet on a new machine): the commands run right
    # after connecting (first-look, send in the new folder) read the index
    refresh()
    return 0


def cmd_rename(old: str, new: str, workspace: str, origin: str, memory_bare: str) -> int:
    reg = hubconf.registry_dir()
    src, dst = reg / f"{old}.yaml", reg / f"{new}.yaml"
    if not src.exists() or dst.exists():
        sys.exit(_("registry rename: {src} must exist and {dst} must not", src=src, dst=dst))
    text = src.read_text()
    doc = yaml.safe_load(text)
    doc["name"] = new
    doc.setdefault("paths", {}).update(workspace=tilde(workspace), origin=origin, memory_bare=tilde(memory_bare))
    # keep the header comments
    head = "".join(l for l in text.splitlines(keepends=True) if l.startswith("#")) if text.startswith("#") else header()
    hubconf.write_atomic(dst, head + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=200), keep=True)
    src.unlink()
    if (reg / old).is_dir():
        (reg / old).rename(reg / new)
    print(_("registry: {old} → {new}", old=old, new=new))
    return 0


def cmd_apply_updates() -> int:
    """manifest.update letters sent by projects (text = registry YAML: the whole entry, or the part that changes;
    manifest_update()): if valid, write it into the registry + commit and reply with what changed as it now stands;
    if not, send the errors back to that project's mailbox; either way the letter is closed. Only the sending
    project's own entry can be changed. Zero LLM."""
    import sqlite3
    bus = hubconf.bus_db()
    if not bus.exists():
        return 0
    hub = hubconf.hub_project()
    c = sqlite3.connect(bus, timeout=30)
    rows = c.execute("SELECT id, source, payload FROM events m WHERE m.type='message' AND m.to_project=?"
                     " AND NOT EXISTS(SELECT 1 FROM events x WHERE x.type='message.done' AND x.ref=m.id) ORDER BY seq",
                     (hub,)).fetchall()
    hubbus = [sys.executable, str(hubconf.ROOT / "bin" / "hubbus")]

    def reply(to, mid, text, note):
        subprocess.run([*hubbus, "send", "--from", hub, "--to", to, "--reply-to", mid, "--type", "info", "--text", said(text)],
                       capture_output=True)
        subprocess.run([*hubbus, "done", mid, "--note", said(note, 400)], capture_output=True, cwd=hubconf.ROOT)

    try:
        index = json.loads((CACHE / "index.json").read_text())
    except (OSError, ValueError):
        index = {"projects": {}}
    for mid, sender, pl in rows:
        p = json.loads(pl or "{}")
        if p.get("type") != "manifest.update":
            continue
        f = hubconf.registry_dir() / f"{sender}.yaml"
        sent = cur = None
        try:   # one letter is returned, whatever is wrong with it; it never stops the others
            cur = (yaml.safe_load(f.read_text()) or {}) if f.exists() else None
            sent = yaml.load(p.get("text") or "", Loader=LetterLoader)
            new, errs = manifest_update(sent, sender, cur, index)
        except yaml.YAMLError as e:
            new, errs = None, [_("cannot read the YAML: {error}", error=e)]
        except Exception as e:   # noqa: BLE001 (an entry the owner left unreadable, a structure too deep)
            new, errs = None, [_("the platform couldn't apply it ({error}); write to brunna", error=f"{type(e).__name__}: {e}"[:200])]
        if errs:
            reply(sender, mid, _("manifest.update was not applied; the registry is unchanged:\n- {errors}", errors="\n- ".join(errs)),
                  _("returned: {errors}", errors="; ".join(errs)[:200]))
            print(_("{sender}: manifest.update {mid} returned ({n} {n:problem|problems})", sender=sender, mid=mid, n=len(errs)))
            continue
        before = tiers.upgrade(cur) if cur is not None else {}
        changed = [k for k in dict.fromkeys([*before, *new]) if before.get(k) != new.get(k)]
        if not changed:
            reply(sender, mid, _("manifest.update changed nothing; the registry entry already says this"), _("no change"))
            print(_("{sender}: manifest.update {mid} changed nothing", sender=sender, mid=mid))
            continue
        index["projects"][sender] = normalise(tiers.upgrade(new))   # the next letter of this run sees its labels
        hubconf.write_atomic(f, "# " + _("Project registry (see bin/registry.py). Last updated by manifest.update {mid}", mid=mid)
                             + "\n" + yaml.safe_dump(new, allow_unicode=True, sort_keys=False, width=200), keep=True)
        r = subprocess.run([str(hubconf.ROOT / "bin" / "commit-hub-files.sh"), f"registry: {sender} manifest.update {mid}",
                            f"projects/{sender}.yaml"], capture_output=True, text=True)
        # what a part changed, as it now stands: a list is replaced whole, so the project keeps what it needs to send
        # next time (a whole entry is the project's own text already)
        now = {} if "v" in sent else as_sent({k: new[k] for k in changed if k in new}, sent)
        gone = [k for k in changed if k not in new]
        reply(sender, mid, _("manifest.update applied") + ("" if r.returncode == 0 else _(" (the commit failed; the file is written)"))
              + _("; it takes effect at the next scheduler tick.")
              + ("\n" + _("Now:") + "\n" + yaml.safe_dump(now, allow_unicode=True, sort_keys=False, width=200).rstrip() if now else "")
              + ("\n" + _("Removed: {keys}", keys=", ".join(gone)) if gone else ""), _("applied"))
        print(_("{sender}: manifest.update {mid} applied", sender=sender, mid=mid))
    return 0


V1_LINE = re.compile(r"^v:\s*1\s*(#.*)?$")
RUNTIME_BLOCK = re.compile(r"^runtime:\s*(#.*)?$")
RUNTIME_FLOW = re.compile(r"^runtime:\s*(\{.*\})\s*(#.*)?$")
TEST_LINE = re.compile(r"^(\s+)test:\s*(.*?)\s*$")


def upgrade_text(text: str) -> str | None:
    """A v1 registry file rewritten as v2 by editing its text, so its comments and layout stay: `v: 2`, runtime.test
    taken out of runtime (an emptied runtime stays as {}), a `full` tier added at the end. None when the file is not
    v1, or when the edited text would not read as exactly tiers.upgrade() of the old one (a form this doesn't know,
    left for a person)."""
    try:
        old = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(old, dict) or old.get("v") != 1:
        return None
    lines = text.splitlines(keepends=True)
    out, test, i = [], None, 0
    while i < len(lines):
        line = lines[i]
        if V1_LINE.match(line.rstrip("\n")):
            out.append(line.replace("1", "2", 1))
        elif RUNTIME_BLOCK.match(line.rstrip("\n")):
            block = []
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i])
                i += 1
            kept = []
            for b in block:
                m = TEST_LINE.match(b.rstrip("\n"))
                if m and test is None:
                    test = m.group(2)
                else:
                    kept.append(b)
            out.append(line if any(b.strip() for b in kept) else line.replace("runtime:", "runtime: {}", 1))
            out += kept
            continue
        elif (m := RUNTIME_FLOW.match(line.rstrip("\n"))):
            rt = yaml.safe_load(m.group(1)) or {}
            if "test" in rt:
                test = json.dumps(rt.pop("test"))
                flow = "{" + ", ".join(f"{k}: {json.dumps(v)}" for k, v in rt.items()) + "}"
                out.append(f"runtime: {flow}" + (f"  {m.group(2)}" if m.group(2) else "") + "\n")
            else:
                out.append(line)
        else:
            out.append(line)
        i += 1
    new = "".join(out)
    if test is not None:
        new = new.rstrip("\n") + f"\ntests:\n- tier: full\n  run: {test}\n  when: [merge, nightly, deploy]\n"
    try:
        return new if yaml.safe_load(new) == tiers.upgrade(old) else None
    except yaml.YAMLError:
        return None


def cmd_upgrade_files(apply: bool) -> int:
    """The registry's v1 files rewritten as v2 (upgrade_text), every one in the registry folder; dry-run unless
    --apply, which commits them with commit-hub-files.sh. Reading already upgrades v1 (validate, refresh): this only
    makes the files say what the platform reads."""
    changed, stuck = [], []
    for f in sorted(hubconf.registry_dir().glob("*.yaml")):
        text = f.read_text()
        try:
            v = (yaml.safe_load(text) or {}).get("v")
        except (yaml.YAMLError, AttributeError):
            v = None
        if v != 1:
            continue
        new = upgrade_text(text)
        if new is None:
            stuck.append(f.name)
            continue
        changed.append(f)
        print(_("{file}: v1 → v2{tests}", file=f.name, tests=_(" (runtime.test → a full tier)") if "\ntests:" in new and "tests:" not in text else ""))
        if apply:
            hubconf.write_atomic(f, new, keep=True)
    for name in stuck:
        print(_("{file}: not rewritten (a form this command does not know; edit it by hand)", file=name), file=sys.stderr)
    if not changed:
        print(_("no v1 registry files to rewrite"))
    elif not apply:
        print(_("(dry-run; add --apply to rewrite and commit them)"))
    else:
        subprocess.run([str(hubconf.ROOT / "bin" / "commit-hub-files.sh"), "registry: files rewritten as project v2",
                        *[f"projects/{f.name}" for f in changed]], check=False)
    return 1 if stuck else 0


def secrets_report(index: dict) -> int:
    """What keeping each project's declared credentials from its confined runs changes (docs/design/project-agent-calls.md
    C): the files and folders, and the confined jobs whose rules start a command, which runs as the user and still
    reads them (only the agent's own file tools are kept off; a sandboxed project's confined run has no command rules).
    Every project's mail handlers are confined runs as well. Prints; returns 0."""
    import policy
    import runtime_ports
    try:
        rt = runtime_ports.load(None, ports=("rule_check",))
    except runtime_ports.Unsupported as e:   # which rules start a command is the runtime's to say
        print(e)
        rt = None
    for name, doc in sorted(index["projects"].items()):
        folders, files = policy.secret_paths(name, doc)
        if not folders and not files:
            continue
        sandboxed = sandbox_enforced(doc)
        print(_("{name} ({how}): {paths}", name=name, how=_("sandboxed") if sandboxed else _("not sandboxed"),
                paths=", ".join(str(p) + ("/" if p in folders else "") for p in [*folders, *files])))
        for job in doc.get("jobs") or []:
            if job.get("kind") != "agent" or not job.get("allow"):
                continue
            commands = rt.command_rules(job["allow"]) if rt else []
            if commands:   # (in a sandboxed project such a job doesn't run: rule_check refuses it)
                print("    " + _("{job}: its rules start {rules}, which still read them", job=job["name"],
                                 rules=", ".join(commands)))
        for agent, decl in sorted((doc.get("agents") or {}).items()):
            commands = rt.command_rules(decl.get("allow") or []) if rt and isinstance(decl, dict) else []
            if commands:
                print("    " + _("{job}: its rules start {rules}, which still read them", job=f"agents.{agent}",
                                 rules=", ".join(commands)))
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["upgrade-files"]:
        return cmd_upgrade_files("--apply" in args)
    if args[:1] == ["add"] and len(args) == 7 and args[5] == "--extra":
        return cmd_add(*args[1:5], extra=args[6])
    if args[:1] == ["add"] and len(args) == 5:
        return cmd_add(*args[1:])
    if args[:1] == ["rename"] and len(args) == 6:
        return cmd_rename(*args[1:])
    if args[:1] == ["apply-updates"]:
        return cmd_apply_updates()
    if args and args[0] == "check":
        args = args[1:]
    if args == ["--secrets-report"]:
        return secrets_report(refresh())
    rebuild = "--rebuild" in args
    args = [x for x in args if x != "--rebuild"]
    index = refresh(rebuild=rebuild)
    names = args or sorted(set(index["projects"]) | set(index["errors"]))
    bad = 0
    for n in names:
        if n in index["errors"]:
            bad += 1
            print(f"✗ {n}" + (_(" (keeping the last valid one)") if n in index["projects"] else ""))
            for e in index["errors"][n]:
                print(f"    {e}")
        elif n in index["projects"]:
            doc = index["projects"][n]
            print(_("✓ {name}: {jobs} {jobs:job|jobs}, {services} {services:service|services}", name=n, jobs=len(doc.get("jobs") or []),
                     services=len(doc.get("services") or [])))
        else:
            bad += 1
            print(_("? {name}: not in the registry", name=n))
    # jobs that don't run although their entry is valid (listed, not counted as invalid)
    for job, why in sorted({**(index.get("refused") or {}), **rules_refused(index)}.items()):
        if not args or job.split("/", 1)[0] in names:
            print(f"! {why}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
