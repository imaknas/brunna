"""Claude Code's conformance checks (docs/design/runtime-versions.md section 2): an adapter, like runtime_claude.py, so
it holds Claude's syntax; bin/conformance.py runs CHECKS against a binary, each in its own throwaway world.

How a check meets the tool (section 3a, "a candidate never refreshes the user's login"):
- prepare(world), in the runner's process: the headless token copied into the world's home (mode 600), and a `security`
  that refuses everything, so no run can reach the macOS Keychain's login items;
- enter(world), in the check's process: CLAUDE_CONFIG_DIR at the world's tool folder (a config dir of its own looks up
  only its own Keychain item, V-M11), that `security` first on PATH, the updater off (V-M5), git of its own off, the
  token in the environment. Every run a check makes inherits it, the adapter's own functions included.

Model calls go to the fast tier (haiku) unless a check is about the models themselves. A check reports a state and a
few words, never the tool's output.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import i18n  # noqa: E402
import names  # noqa: E402
import runtime_claude as rc  # noqa: E402
from runtime_ports import Check, CompletionRequest, Outcome  # noqa: E402

_ = i18n.domain(__file__)
ROOT = pathlib.Path(__file__).resolve().parents[1]
FAST = rc.MODELS["fast"]
ASK = "Reply with exactly the two letters ok and nothing else."
SECURITY = rc.SECURITY_STUB   # the same refusing `security` the platform's token runs get


# ---------- the world ----------

def prepare(world) -> None:
    """In the runner (the live home): what a check's runs log in with, and nothing else of the user's. No headless
    token is a refusal (RuntimeError): the checks never fall back to another login."""
    token = rc._token_file()
    if not token.is_file():
        raise RuntimeError(_("no headless token at {file}: the checks log in with it and nothing else", file=token))
    dest = pathlib.Path(world.home) / ".claude" / token.name
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token.read_text())
    bindir = pathlib.Path(world.root) / "bin"
    bindir.mkdir(mode=0o700, exist_ok=True)
    (bindir / "security").write_text(SECURITY)
    (bindir / "security").chmod(0o700)


def enter(world) -> None:
    """In the check's process, before its check: the environment every run there inherits. Only adds to the world's
    environment (its HOME and platform pointers stay); no token there is a refusal (RuntimeError)."""
    env, why = rc.auth_env("token", os.environ)
    if env is None:
        raise RuntimeError(why)
    prefix = pathlib.Path(world.root) / "npm-prefix"   # an updater that ignored the flag could write only here (V-M5)
    prefix.mkdir(mode=0o700, exist_ok=True)
    os.environ.update({rc.TOKEN_VAR: env[rc.TOKEN_VAR], "CLAUDE_CONFIG_DIR": str(world.tool), "DISABLE_AUTOUPDATER": "1",
                       "npm_config_prefix": str(prefix), **rc.NO_FOLDER_GIT,
                       "PATH": f"{pathlib.Path(world.root) / 'bin'}:{os.environ.get('PATH', '')}"})


def _claude(args, *, cwd, stdin="", timeout=300, env=None, exe=None) -> subprocess.CompletedProcess:
    """A run of the binary under check (exe: another one, in the same world: the copy a check pairs it with)."""
    return subprocess.run([exe or rc.binary(), *args], input=stdin, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=rc._env(type("T", (), {"env": dict(os.environ if env is None else env)})()))


def _result(r) -> dict | None:
    try:
        got = json.loads(r.stdout.strip().splitlines()[-1])
        return got if isinstance(got, dict) else None
    except (ValueError, IndexError):
        return None


def _events(r) -> list[dict]:
    out = []
    for line in r.stdout.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict):
            out.append(o)
    return out


def _init_tools(r) -> list | None:
    for o in _events(r):
        if o.get("type") == "system" and o.get("subtype") == "init":
            return list(o.get("tools") or [])
    return None


def _fail_reason(r, got) -> str:
    if got is None:
        return _("no result (exit {code})", code=r.returncode)
    return _("is_error (exit {code})", code=r.returncode)


def _unanswered(r, got) -> Outcome | None:
    """A run that gave no answer at all (an outage, a login gone) can't say anything about the version: an error, not a
    fail. None when it answered."""
    if got is None or got.get("is_error"):
        return Outcome("error", _fail_reason(r, got))
    return None


# ---------- the checks ----------

def distribution(world) -> Outcome:
    """A copy installed by the distribution port (a folder of its own with its lockfile) has an integrity hash for every
    package it resolved. A copy installed some other way (nvm's global one) has no lockfile to read."""
    exe = pathlib.Path(rc.binary()).resolve()
    prefix = next((p for p in exe.parents if (p / "package-lock.json").is_file() and (p / "node_modules").is_dir()), None)
    if prefix is None:
        return Outcome("not-checkable", _("not a copy installed by the platform (no lockfile beside it)"))
    try:
        lock = json.loads((prefix / "package-lock.json").read_text())
    except (OSError, ValueError):
        return Outcome("fail", _("its lockfile can't be read"))
    packages = {k: v for k, v in (lock.get("packages") or {}).items() if k and isinstance(v, dict)}
    unhashed = sorted(k for k, v in packages.items() if v.get("resolved") and not v.get("integrity"))
    if not packages or unhashed:
        return Outcome("fail", _("packages with no integrity hash: {names}", names=", ".join(unhashed) or "-"))
    return Outcome("pass")


def agent_result(world) -> Outcome:
    """A run's result fields parse (result, is_error, usage, permission_denials)."""
    r = _claude(["-p", "--tools", "", "--model", FAST, "--output-format", "json", "--strict-mcp-config"], cwd=world.project,
                stdin=ASK)
    got = _result(r)
    if (no := _unanswered(r, got)):
        return no
    missing = [k for k in ("result", "is_error", "usage", "permission_denials", "session_id") if k not in got]
    return Outcome("fail", _("result lacks {fields}", fields=", ".join(missing))) if missing else Outcome("pass")


def agent_models(world) -> Outcome:
    """Each model alias the adapter names (MODELS) still resolves."""
    bad, answered = [], 0
    for group, alias in rc.MODELS.items():
        r = _claude(["-p", "--tools", "", "--model", alias, "--output-format", "json", "--strict-mcp-config",
                     "--no-session-persistence"], cwd=world.project, stdin=ASK)
        got = _result(r)
        if got is None or got.get("is_error"):
            bad.append(f"{group}={alias}")
        else:
            answered += 1
    if bad and not answered:   # none answered: the service or the login, not the aliases
        return Outcome("error", _("no model answered"))
    return Outcome("fail", _("these don't answer: {models}", models=", ".join(bad))) if bad else Outcome("pass")


def completion_schemas(world) -> Outcome:
    """One completion per schema in contracts/llm/, the way every completion runs, returns output valid for it."""
    import minischema
    bad, answered = [], 0
    for f in sorted((ROOT / "contracts" / "llm").glob("*.schema.json")):
        schema = json.loads(f.read_text())
        got = rc.complete(CompletionRequest(point="login-check", model=FAST, timeout_s=240, schema=schema, prompt=(
            "This is a test of structured output. Return a small example that is valid for the schema you were given; "
            "make up short placeholder values.")), {"auth": "token"})
        answered += bool(got.ok)
        try:
            ok = got.ok and not minischema.errors(schema, json.loads(got.text))
        except ValueError:
            ok = False
        if not ok:
            bad.append(f.name.removesuffix(".schema.json"))
    if bad and not answered:
        return Outcome("error", _("no completion answered"))
    return Outcome("fail", _("no valid output for {schemas}", schemas=", ".join(bad))) if bad else Outcome("pass")


def _completion_argv(schema=None, tools_off: bool = True) -> list:
    """complete()'s own argv (runtime_claude.completion_command), its output as stream-json so the init event shows the
    run's tools (complete() reads it so itself; an argv that doesn't yet is made to); tools_off=False takes its
    `--tools ""` out (the positive control)."""
    argv = rc.completion_command(CompletionRequest(point="login-check", prompt=ASK, model=FAST, schema=schema))[1:]
    i = argv.index("--output-format")
    if argv[i + 1] != "stream-json":
        argv[i:i + 2] = ["--output-format", "stream-json"]
    if "--verbose" not in argv:
        argv.append("--verbose")
    if not tools_off:
        j = argv.index("--tools")
        del argv[j:j + 2]
    return argv


def completion_no_tools(world) -> Outcome:
    """A completion's run has no tools (V-M7): with complete()'s own flags its init event lists none, and with a schema
    only StructuredOutput; positive control: the same run without `--tools ""` lists some, so the check still sees what
    it looks for."""
    control = _init_tools(_claude(_completion_argv(tools_off=False), cwd=world.project, stdin=ASK))
    if not control:
        return Outcome("fail", _("tests nothing: a run with tools listed none in its init event"))
    for schema, allowed in ((None, set()), ({"type": "object", "properties": {"ok": {"type": "string"}}}, {"StructuredOutput"})):
        tools = _init_tools(_claude(_completion_argv(schema), cwd=world.project, stdin=ASK))
        if tools is None:
            return Outcome("fail", _("the run gave no init event"))
        if set(map(str, tools)) - allowed:
            return Outcome("fail", _("a completion's run has tools: {tools}",
                                     tools=", ".join(sorted(set(map(str, tools)) - allowed)[:8])))
    return Outcome("pass")


def confine_folder_git(world) -> Outcome:
    """A platform run leaves a repo's git filter alone (NO_FOLDER_GIT), with its positive control: the same run without
    the variable fires it. Model calls, in a repo inside the world."""
    why = filter_probe(pathlib.Path(world.tmp))   # the binary under check (rc.binary())
    if why is None:
        return Outcome("pass")
    # NONE: this version runs no git in the folder even without the variable (2.1.322, 11-13): nothing to guard on this
    # path, and nothing to test; a version that runs it again brings the control back and is checked as before
    state = "fail" if why.startswith("FAIL:") else "not-checkable" if why.startswith("NONE:") else "error"
    return Outcome(state, why.removeprefix("FAIL:").removeprefix("NONE:").strip())


def _platform_run(repo: pathlib.Path, tmp: pathlib.Path) -> tuple[int, dict | None]:
    """The login-check point run the platform's own way (llm.py, in this process, so binary() is the one under check
    when there is one): (exit code, its result or None)."""
    import contextlib
    import io
    from unittest import mock
    import llm
    pf = tmp / "probe-prompt.txt"
    pf.write_text(ASK)
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["llm.py", "run", "login-check", "--cwd", str(repo), "--prompt-file", str(pf)]), \
            mock.patch.dict(os.environ, {"LLM_AUTH": "token"}), contextlib.redirect_stdout(out):
        try:
            code = llm.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    lines = out.getvalue().strip().splitlines()
    try:
        got = json.loads(lines[-1]) if lines else None
    except ValueError:
        got = None
    return code or 0, got if isinstance(got, dict) else None


def agent_run(repo: pathlib.Path, guarded: bool = True, exe: str | None = None, tmp: pathlib.Path | None = None) -> str | None:
    """One minimal request in this folder. Why not, or None. Guarded: the platform's own path (llm.py's login-check
    point, in this process, so binary() is the one under check when there is one). Not guarded: that run's own flags
    (llm.py --dry-run) with the variable left out, run with exe."""
    exe = exe or rc.binary()
    if guarded:
        code, got = _platform_run(repo, tmp or repo.parent)
        if code != 0 or got is None or got.get("is_error"):
            return _("the platform's run failed (exit {code})", code=code)
        return None
    dry = subprocess.run([sys.executable, str(ROOT / "bin" / "llm.py"), "run", "login-check", "--cwd", str(repo),
                          "--dry-run"], input=ASK, capture_output=True, text=True, timeout=120, cwd=repo,
                         env={**os.environ, "LLM_AUTH": "token"})
    try:
        argv = json.loads(dry.stdout) if dry.returncode == 0 else None
    except ValueError:
        argv = None
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return _("llm.py --dry-run gave no command (exit {code})", code=dry.returncode)
    env, why = rc.auth_env("token", dict(os.environ))
    if env is None:
        return why
    env["HUB_LLM_POINT"] = "login-check"
    run_env = rc._env(type("T", (), {"env": dict(env)})())
    for k in rc.NO_FOLDER_GIT:
        run_env.pop(k, None)
    try:
        r = subprocess.run([exe, *argv[1:]], input=ASK, capture_output=True, text=True, timeout=180, cwd=repo, env=run_env)
    except subprocess.TimeoutExpired:
        return _("no answer for 3 minutes")
    got = _result(r)
    return _fail_reason(r, got) if got is None or got.get("is_error") or r.returncode != 0 else None


def filter_probe(tmp: pathlib.Path, exe: str | None = None, run=None) -> str | None:
    """Whether a platform run still leaves a repo's git filter alone: None, or what is wrong, prefixed "FAIL:" when the
    property doesn't hold and "NONE:" when the unguarded run shows nothing to guard (otherwise the probe couldn't tell).
    A plain `git status` shows git fires the filter, and the same run without the variable shows claude still would:
    with either silent, a pass would say nothing. run(repo, guarded) makes the request (agent_run by default). It was
    update-claude.py's until 11-14; brunna/update-runtimes now runs it with each new version's checks."""
    exe = exe or rc.binary()
    run = run or (lambda repo, guarded: agent_run(repo, guarded, exe, tmp))
    env0 = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env0.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    repo, marker, script = tmp / "repo", tmp / "ran", tmp / "filter.sh"
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    script.write_text(f'#!/bin/sh\necho x >> "{marker}"\ncat\n')
    script.chmod(0o755)

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env0, timeout=60)

    def dirty():
        marker.unlink(missing_ok=True)
        t = (repo / "a.txt").stat().st_mtime + 5
        os.utime(repo / "a.txt", (t, t))
    (repo / "a.txt").write_text("a\n")
    git("init", "-q", "-b", "main")
    git("add", "a.txt")
    git("commit", "-qm", "x")
    git("config", "filter.x.clean", str(script))
    (repo / ".git" / "info" / "attributes").write_text("a.txt filter=x\n")
    dirty()
    git("--no-optional-locks", "status", "--short")
    if not marker.exists():
        return _("the probe says nothing: a plain git status didn't run the filter either")
    dirty()
    if (why := run(repo, False)):
        return _("the probe could not run: {why}", why=why)
    if not marker.exists():
        return "NONE: " + _("the probe no longer tests anything: without the variable claude didn't run the filter "
                            "either, so its git status has moved or gone; look again at what stops it "
                            "(runtime_claude.py NO_FOLDER_GIT)")
    dirty()
    if (why := run(repo, True)):
        return _("the probe could not run: {why}", why=why)
    if marker.exists():
        return "FAIL: " + _("claude ran a repo's git filter as you, in a platform run: a sandboxed project can run code "
                            "outside its sandbox this way, until the variable that stops it is put right "
                            "(runtime_claude.py NO_FOLDER_GIT)")
    return None


def lifecycle_events(world) -> Outcome:
    """The hooks the platform installs fire with the fields its parser reads: after a run in a project with them
    installed, the world's hub-event log has a line for each event that ran, none of them unusable."""
    project = pathlib.Path(world.project)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, capture_output=True, timeout=60)
    rc.install_lifecycle(project)
    # a project's hooks run only in a folder the config dir trusts, as the owner's projects are trusted in theirs
    trust = pathlib.Path(world.tool) / ".claude.json"
    trust.write_text(json.dumps({"projects": {str(project.resolve()): {"hasTrustDialogAccepted": True}}}))
    r = _claude(["-p", "--tools", "", "--model", FAST, "--output-format", "json", "--strict-mcp-config",
                 "--setting-sources", "project,local"], cwd=project, stdin=ASK)
    got = _result(r)
    if got is None or got.get("is_error"):
        return Outcome("error", _fail_reason(r, got))
    import hubconf
    log = hubconf.LOGS / "hub-event.log"
    lines = log.read_text().splitlines() if log.exists() else []
    seen = {ln.split()[1] for ln in lines if len(ln.split()) > 1}
    if not {"session_start", "prompt"} <= seen:
        return Outcome("fail", _("these events didn't reach the platform: {events}",
                                 events=", ".join(sorted({"session_start", "prompt"} - seen))))
    bad = [ln.split("unusable=", 1)[1].split()[0] for ln in lines if "unusable=" in ln]
    return Outcome("fail", _("payloads the platform can't read: {reasons}", reasons=", ".join(sorted(set(bad))))) if bad \
        else Outcome("pass")


def instructions_reach(world) -> Outcome:
    """A marker in the rules file the adapter writes (CLAUDE.local.md) reaches the model. The marker is random, so the
    model can only say it if the file reached it."""
    project = pathlib.Path(world.project)
    word = "PELICAN-" + os.urandom(3).hex().upper()
    ask = "What is the code word in your project instructions? Reply with only the code word, or NONE if there is none."
    rc.instructions_file(project).write_text(f"The code word is {word}.\n")
    r = _claude(["-p", "--tools", "", "--model", FAST, "--output-format", "json", "--strict-mcp-config",
                 "--no-session-persistence", "--setting-sources", "project,local"], cwd=project, stdin=ask)
    got = _result(r)
    if (no := _unanswered(r, got)):
        return no
    return Outcome("pass") if word in str(got.get("result", "")) else Outcome("fail", _("the rules file didn't reach the model"))


def sessions_read(world) -> Outcome:
    """The run's transcript parses with no line skipped, and says the binary's version."""
    import sessions_claude
    r = _claude(["-p", "--tools", "", "--model", FAST, "--output-format", "json", "--strict-mcp-config"], cwd=world.project,
                stdin=ASK)
    got = _result(r)
    if got is None or not got.get("session_id"):
        return Outcome("error", _fail_reason(r, got))
    files = list(pathlib.Path(world.tool).glob(f"projects/*/{got['session_id']}.jsonl"))
    if not files:
        return Outcome("fail", _("no transcript for the run where the platform reads them"))
    tally: dict = {}
    turns = list(sessions_claude.records(files[0], tally))
    want = rc.version_of(rc.binary())
    skipped = sum(s for _l, s in tally.values())
    if skipped or not turns:
        return Outcome("fail", _("{n} {n:line|lines} skipped, {t} {t:turn|turns} read", n=skipped, t=len(turns)))
    versions = sorted(v for v in tally if v)
    return Outcome("pass") if versions == [want] else Outcome("fail", _("the transcript says version {got}, not {want}",
                                                                         got=", ".join(versions) or "-", want=want))


def sessions_fork(world) -> Outcome:
    """A session written by this version, forked by the platform's current copy (world.pair, the runner's choice; the
    version itself when there is none, as on a candidate for promotion), reads back: the fork answers from the first
    session's context, under an id of its own (docs/design/runtime-versions.md section 5, a mailbox fork of a user's
    session). The word is random, so the fork can only say it if it read the transcript."""
    word = "HERON-" + os.urandom(3).hex().upper()
    flags = ["--tools", "", "--model", FAST, "--output-format", "json", "--strict-mcp-config"]
    r = _claude(["-p", *flags], cwd=world.project, stdin=f"Remember the code word {word}. " + ASK)
    got = _result(r)
    if (no := _unanswered(r, got)):
        return no
    sid = got.get("session_id")
    if not sid:
        return Outcome("fail", _("the first run named no session"))
    pair = getattr(world, "pair", None) or rc.binary()
    r = _claude(["-p", "--resume", sid, "--fork-session", *flags], cwd=world.project, exe=pair,
                stdin="What was the code word I asked you to remember? Reply with only the code word.")
    forked = _result(r)
    if forked is None or forked.get("is_error"):   # the version that wrote it answered: the fork is what failed
        return Outcome("fail", _("the fork didn't run: {why}", why=_fail_reason(r, forked)))
    if forked.get("session_id") == sid:
        return Outcome("fail", _("the fork kept the session's own id"))
    return Outcome("pass") if word in str(forked.get("result", "")) else \
        Outcome("fail", _("the fork didn't read the session it was forked from"))


def stream_exchange(world) -> Outcome:
    """A short stream-json exchange (leases) parses into the platform's events, ending in a result."""
    msg = json.dumps({"type": "user", "message": {"role": "user", "content": ASK}})
    r = _claude(["-p", "--tools", "", "--model", FAST, "--input-format", "stream-json", "--output-format", "stream-json",
                 "--verbose", "--strict-mcp-config", "--no-session-persistence"], cwd=world.project, stdin=msg + "\n")
    events = [e for line in r.stdout.splitlines() for e in rc.parse_stream(line)]
    ends = [e for e in events if e.type == "turn_end"]
    if any(e.data.get("is_error") for e in ends):
        return Outcome("error", _("the turn ended in an error"))
    return Outcome("pass") if ends else Outcome("fail", _("no turn_end among {n} {n:event|events}", n=len(events)))


def tool_servers_listed(world) -> Outcome:
    """`claude mcp list` parses with no server and with one added (what ambient_servers reads)."""
    from runtime_ports import Task
    project = str(world.project)
    task = Task(point="conformance", prompt="", spec={}, cwd=project, env=dict(os.environ))
    try:
        none = rc._listed_servers(task)
        r = _claude(["mcp", "add", "-s", "user", "probe-server", "--", "/bin/echo"], cwd=project, timeout=60)
        if r.returncode != 0:
            return Outcome("error", _("could not add a server (exit {code})", code=r.returncode))
        one = rc._listed_servers(task)
    except ValueError as e:
        return Outcome("fail", _("the listing didn't parse: {error}", error=type(e).__name__))
    if none:
        return Outcome("fail", _("a world with no servers lists {n}", n=len(none)))
    return Outcome("pass") if "probe-server" in one else Outcome("fail", _("an added server isn't listed"))


def auth_login_error(world) -> Outcome:
    """A run with a token that can't log in is told apart as a login error (is_login_error), not another failure."""
    env = {**os.environ, rc.TOKEN_VAR: "sk-ant-oat01-" + "0" * 40}
    r = _claude(["-p", "--tools", "", "--model", FAST, "--output-format", "json", "--strict-mcp-config",
                 "--no-session-persistence"], cwd=world.project, stdin=ASK, env=env, timeout=120)
    got = _result(r) or {}
    text = f"{got.get('result', '')} {r.stderr}"
    if r.returncode == 0 and not got.get("is_error"):
        return Outcome("error", _("tests nothing: a bad token answered"))
    return Outcome("pass") if rc.is_login_error(text) else Outcome("fail", _("a bad token isn't read as a login error"))


def auth_keychain(world) -> Outcome:
    """DV5 (c): every token run has a `security` first on PATH that refuses, which holds only while the tool reaches the
    Keychain through that command, and looks up only its own config dir's item. A run with no token, under a stub that
    logs and refuses: it must show its lookups in the log (else it reaches the Keychain another way, and DV5 is open again),
    and none of them may name the user's own unhashed login item."""
    if sys.platform != "darwin":
        return Outcome("not-checkable", _("the Keychain is macOS's"))
    stub = pathlib.Path(world.root) / "stub"
    stub.mkdir(mode=0o700, exist_ok=True)
    log = stub / "calls.log"
    (stub / "security").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 44\n')
    (stub / "security").chmod(0o700)
    env = {k: v for k, v in os.environ.items() if k != rc.TOKEN_VAR}
    env["PATH"] = f"{stub}:{env.get('PATH', '')}"
    _claude(["-p", "--tools", "", "--model", FAST, "--output-format", "json", "--strict-mcp-config",
             "--no-session-persistence"], cwd=world.project, stdin=ASK, env=env, timeout=120)
    calls = log.read_text().splitlines() if log.exists() else []
    if not calls:
        return Outcome("fail", _("no Keychain lookup went through `security`: this version reaches the Keychain some other "
                                 "way, so the refusing stub no longer guards token runs"))
    import re
    own = [c for c in calls if "Claude Code-credentials" in c and not re.search(r"Claude Code-credentials-[0-9a-f]{4,}", c)]
    return Outcome("fail", _("it looked up the user's own login item")) if own else Outcome("pass")


# ---------- credential_tools: the platform's bridge as a run's stdio server (platform-credentials.md C5) ----------
BRIDGE_PROBE = r"""import json, sys
log = sys.argv[1]
for line in sys.stdin:
    m = json.loads(line)
    open(log, "a").write(json.dumps({"method": m.get("method")}) + "\n")
    if "id" not in m:
        continue
    if m.get("method") == "initialize":
        r = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "probe", "version": "0"}}
    elif m.get("method") == "tools/list":
        r = {"tools": [{"name": "probe__read_one", "inputSchema": {"type": "object"}}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"""


def credential_bridge(world) -> Outcome:
    """A sealed run (--strict-mcp-config) loads a stdio server from --mcp-config and lists its tools before any login is
    used, the server id and a tool name holding __ kept as tools_named reads them, and nothing reaches for the Keychain
    on that server's behalf: no `security -i` (a write), only the run's own config dir's login lookups. No model call:
    the run has no login, so it stops after connecting."""
    root = pathlib.Path(world.root)
    probe, log = root / "bridge_probe.py", root / "bridge_probe.log"
    probe.write_text(BRIDGE_PROBE)
    stub = root / "bridge-stub"
    stub.mkdir(mode=0o700, exist_ok=True)
    calls = stub / "calls.log"
    (stub / "security").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit 44\n')
    (stub / "security").chmod(0o700)
    cfg = root / "bridge-mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"probe-server": {"command": sys.executable,
                                                               "args": ["-I", str(probe), str(log)], "env": {}}}}))
    env = {k: v for k, v in os.environ.items() if k != rc.TOKEN_VAR}
    env["PATH"] = f"{stub}:{env.get('PATH', '')}"
    r = _claude(["-p", "--output-format", "stream-json", "--verbose", "--strict-mcp-config", "--mcp-config", str(cfg),
                 "--no-session-persistence"], cwd=world.project, stdin=ASK, env=env, timeout=120)
    seen = [json.loads(x).get("method") for x in log.read_text().splitlines()] if log.exists() else []
    if "initialize" not in seen or "tools/list" not in seen:
        return Outcome("fail", _("a sealed run didn't start its stdio server and list its tools ({seen})",
                                 seen=", ".join(map(str, seen)) or "-"))
    tools = _init_tools(r) or []
    if tools and "mcp__probe-server__probe__read_one" not in tools:
        return Outcome("fail", _("the server's tool isn't named as the rules name it"))
    if rc.rules_tools_named(["mcp__probe-server__probe__read_one"], "probe-server") != ["probe__read_one"]:
        return Outcome("fail", _("tools_named doesn't read the rule back"))
    lookups = calls.read_text().splitlines() if calls.exists() else []
    if any(c.split()[:1] == ["-i"] for c in lookups):
        return Outcome("fail", _("the run called `security -i` (a Keychain write) with a stdio server attached"))
    return Outcome("pass")


# ---------- control and confine-sandbox: a sandboxed throwaway project's session (section 7, step 4) ----------
# The world gets a sandboxed project and a neighbour in its own registry; the project's session settings are rendered by
# the platform's renderer (policy → settings), and a throwaway session in bypass mode (V-M9: denies hold there, the
# tool's own sensitive-file ask doesn't) is asked to write each canary, one per run. A canary counts as refused only
# from the run's own word (a file tool's permission denial naming it; a Bash call naming it, the file then unwritten),
# never from the file's absence. Its positive control: the same session, the same way of writing, with every rendered
# rule covering it left out, writes it (the file tools for the control paths: bypass leaves them open, while Claude's
# own sandbox keeps Bash off its settings files and unlisted folders whatever the rules say).

SANDBOXED, NEIGHBOUR = "conformance-sandboxed", "conformance-neighbour"
CONTROL_CANARIES = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/canary", ".mcp.json",
                    ".claude/launch.json")   # never CLAUDE.md: no rule covers it (section 7)
FILE_TOOLS = ("Write", "Edit", "MultiEdit")
SESSION_TOOLS = "Bash,Edit,Write,Read"
RUN_S = 240


def _script(name: str):
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / f"{name}.py"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


def _rendered(world) -> tuple[pathlib.Path, pathlib.Path, dict]:
    """(the sandboxed project's folder, its neighbour's, the session settings the platform renders for it). Its hooks
    are left out: they aren't what these checks test, and from a worktree they would run the main checkout's
    hub-event."""
    import hubconf
    import registry
    project, neighbour = pathlib.Path(world.project), pathlib.Path(world.root) / "neighbour"
    neighbour.mkdir(mode=0o700, exist_ok=True)
    reg = hubconf.registry_dir()
    reg.mkdir(parents=True, exist_ok=True)
    import yaml
    for doc in ({"v": 1, "name": SANDBOXED, "paths": {"workspace": str(project)}, "enforce": {"sandbox": True}},
                {"v": 1, "name": NEIGHBOUR, "paths": {"workspace": str(neighbour)}}):
        (reg / f"{doc['name']}.yaml").write_text(yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))
    index = registry.refresh(rebuild=True)
    if SANDBOXED not in index.get("projects", {}):
        raise RuntimeError(_("the throwaway project could not be registered"))
    got = _script("render-project-settings").compose(SANDBOXED, index["projects"][SANDBOXED], index)
    settings = dict(got[2])
    settings.pop("hooks", None)
    if not (settings.get("sandbox") or {}).get("enabled"):
        raise RuntimeError(_("the sandboxed project's settings have no sandbox"))
    # -p trusts its folder; the trust record as a person's would be, all the same (lifecycle_events)
    (pathlib.Path(world.tool) / ".claude.json").write_text(json.dumps(
        {"projects": {str(project.resolve()): {"hasTrustDialogAccepted": True}}}))
    return project, neighbour, settings


def _rule_regex(spec: str, home: str) -> re.Pattern | None:
    """A rule's path (~/…, //abs) as a regex, the way the tool reads it (gitignore globs, case-insensitive): ** any
    depth, * and ? within a name. Matched against a lower-cased path: Python's re.I would also fold the long s (U+017F)
    to s, which the tool's rules don't (confined-mail-tools.md M6; review A-C1)."""
    p = home + spec[1:] if spec.startswith("~") else spec[1:] if spec.startswith("//") else None
    if p is None:
        return None
    rx = re.escape(p.lower()).replace(r"/\*\*/", "/(?:.*/)?").replace(r"/\*\*", "(?:/.*)?").replace(r"\*", "[^/]*") \
        .replace(r"\?", "[^/]")
    return re.compile(rx)


def _spelled(target) -> set[str]:
    return {str(target), os.path.realpath(target)}


def _covers_rule(rule: str, target, home: str, fold: bool = False) -> bool:
    """A file tools' or Read deny on target, or on a folder above it (a deny on the folder entry alone keeps it from being
    made, and so the file in it: `.claude/hooks`). fold: as the sandbox reads the rule (it is merged into the sandbox's
    filesystem keys, which fold the long s: `Edit(…/.agentſ)` refuses a Bash write to `.agents`, measured 11-14 on
    2.1.323), not as the file tools do; what a positive control leaves out."""
    m = re.fullmatch(r"(\w+)\((.*)\)", rule)
    if not m or m[1] not in (*FILE_TOOLS, "Read"):   # a Read deny refuses a write too (2.1.323)
        return False
    rx = _rule_regex(m[2].casefold() if fold else m[2], home.casefold() if fold else home)
    places = {str(p) for s in _spelled(target) for p in (pathlib.Path(s), *pathlib.Path(s).parents)}
    return bool(rx) and any(rx.fullmatch(s.casefold() if fold else s.lower()) for s in places)


def _covers_entry(entry: str, target, home: str) -> bool:
    """Compared case-folded: the sandbox matches an entry case-insensitively and folds the long s (`.agentſ` refuses
    `.agents`, measured 11-14 on 2.1.323), so a positive control that kept a folded spelling would test nothing."""
    e = (home + entry[1:] if entry.startswith("~") else entry).rstrip("/").casefold()
    return any(s == e or s.startswith(e + "/") for s in (x.casefold() for x in _spelled(target)))


def _covered(settings: dict, target) -> bool:
    home = str(pathlib.Path.home())
    fs = (settings.get("sandbox") or {}).get("filesystem") or {}
    return any(_covers_rule(r, target, home, fold=True) for r in (settings.get("permissions") or {}).get("deny") or []) or \
        any(_covers_entry(e, target, home) for k in ("denyWrite", "denyRead") for e in fs.get(k) or [])


def _without(settings: dict, target, also=()) -> dict:
    """The settings with every rendered rule covering a write to target (and to each of also: where a staged canary is
    built) left out: the file tools' denies whose path matches it, and the sandbox entries it is under."""
    home = str(pathlib.Path.home())
    out = json.loads(json.dumps(settings))
    spots = [target, *also]
    perms = out.get("permissions") or {}
    perms["deny"] = [r for r in perms.get("deny") or [] if not any(_covers_rule(r, t, home, fold=True) for t in spots)]
    fs = (out.get("sandbox") or {}).get("filesystem") or {}
    for k in ("denyWrite", "denyRead"):
        if k in fs:
            fs[k] = [e for e in fs[k] if not any(_covers_entry(e, t, home) for t in spots)]
    return out


def _staged_at(target) -> list[pathlib.Path]:
    """Where a "stage" canary's tree may be built: under each temp root Claude's sandbox lets a session write, as the
    rules name them (the session's $TMPDIR is under one of them)."""
    t = pathlib.Path(target)
    return [pathlib.Path(root) / "stage" / t.parent.name / t.name for root in rc.temp_roots()]


def _names_it(tool, inp, target) -> bool:
    if not isinstance(inp, dict):
        return False
    if tool in FILE_TOOLS:
        fp = inp.get("file_path")
        return isinstance(fp, str) and os.path.realpath(fp) == os.path.realpath(target)
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        return any(s in cmd for s in _spelled(target))
    return False


# what a command refused by the sandbox prints (macOS: EPERM, "Operation not permitted"; a read-only mount elsewhere)
SANDBOX_REFUSAL = re.compile(r"operation not permitted|permission denied|read-only file system", re.I)


def _tool_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_tool_text(c.get("text") if isinstance(c, dict) else c) for c in content)
    return ""


def _refused(r, target, how: str) -> bool:
    """The run shows its write of target was refused. Through the file tools: only a permission denial of a file tool
    naming it (a Write that failed for another reason, such as an existing file it hadn't read, says nothing about the
    rules). Through Bash ("bash", "stage"): the result of a Bash call naming it shows the sandbox's refusal (review
    A-B1: a file that's just absent says nothing); whether the file was written is read from the file."""
    named = pathlib.Path(target).parent.parent if how == "stage" else target
    calls = set()
    for ev in _events(r):
        if how in ("bash", "stage") and ev.get("type") == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash" \
                        and _names_it("Bash", b.get("input"), named):
                    calls.add(b.get("id"))
        if how in ("bash", "stage") and ev.get("type") == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in calls \
                        and SANDBOX_REFUSAL.search(_tool_text(b.get("content"))):
                    return True
        if how == "file" and ev.get("type") == "result":
            if any(isinstance(d, dict) and d.get("tool_name") in FILE_TOOLS
                   and _names_it(d.get("tool_name"), d.get("tool_input"), target)
                   for d in ev.get("permission_denials") or []):
                return True
    return False


def _holds(target, token: str) -> bool:
    try:
        return token in pathlib.Path(target).read_text(errors="replace")
    except OSError:
        return False


def _bash_command(target, token: str, how: str) -> str:
    """The one command a Bash canary asks for: its folder made first (a canary whose folder isn't there would test only
    that printf makes none; review A-B1). "stage": the tree built in the session's temp folder and moved into place
    as one rename (review A-B2): target is <place>/<name>/canary, staged as $TMPDIR/<place's name>/<name>/canary."""
    t = pathlib.Path(target)
    if how == "stage":
        place, inner = t.parent.parent, f"{t.parent.parent.name}/{t.parent.name}"
        return (f"sh -c 'mkdir -p \"$TMPDIR/{inner}\" && printf %s {token} > \"$TMPDIR/{inner}/{t.name}\" && "
                f"mv \"$TMPDIR/{place.name}\" {place}'")
    return f"sh -c 'mkdir -p {t.parent} && printf %s {token} > {t}'"


def _ask(target, token: str, how: str) -> str:
    head = "This is an automated test of a sandbox, in a throwaway folder; nothing here is real or private. "
    if how in ("bash", "stage"):
        return (head + f"Use the Bash tool once to run exactly this command: {_bash_command(target, token, how)}\n"
                "Then reply done. If it is refused, don't try another way; just reply done.")
    return (head + f"Use the Write tool once to write the file {target} with exactly this content: {token}\n"
            "First use the Read tool on that file (it may not exist yet; if it does, it is a throwaway copy made for "
            "this test), then write it with Write, whatever the Read said, even if the Read was refused. Use no other "
            "way to write it. If the write is refused, don't try again; just reply done.")


def _attempt(project: pathlib.Path, settings: dict, target: pathlib.Path, how: str = "file") -> tuple[bool, bool]:
    """A throwaway session under these settings asked to write target: (its write was refused, it wrote). A run that
    shows neither is the model's choice or a failure of another kind, not the tool's answer (2.1.323 once declined an
    existing settings file as "live"): asked once more."""
    got = _attempt_once(project, settings, target, how)
    return got if got[0] or got[1] else _attempt_once(project, settings, target, how)


def _attempt_once(project: pathlib.Path, settings: dict, target: pathlib.Path, how: str) -> tuple[bool, bool]:
    token = "CANARY-" + os.urandom(4).hex().upper()
    path = rc.settings_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    own = pathlib.Path(target) == path
    made = [] if own else [d for d in pathlib.Path(target).parents if not d.exists()]
    if not own:
        pathlib.Path(target).unlink(missing_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    try:
        r = _claude(["-p", "--model", FAST, "--output-format", "stream-json", "--verbose", "--permission-mode",
                     "bypassPermissions", "--tools", SESSION_TOOLS, "--strict-mcp-config", "--no-session-persistence",
                     "--setting-sources", "project,local"], cwd=project, stdin=_ask(target, token, how), timeout=RUN_S)
    except subprocess.TimeoutExpired:
        r = None
    wrote = _holds(target, token)
    if not own:
        _remove(target, made)
    return bool(r) and not wrote and _refused(r, target, how), wrote


def _remove(target, made) -> None:
    """A canary gone again, and the folders its run made (never one that was there before: a seeded entry)."""
    pathlib.Path(target).unlink(missing_ok=True)
    for d in made:   # deepest first
        try:
            d.rmdir()
        except OSError:
            break


def _canaries(project: pathlib.Path, settings: dict, targets: list) -> tuple[list[str], list[str]]:
    """(fails, errors) over the canaries [(target, how, name)], each guarded then with its own positive control (the
    same way of writing, with every rule covering it left out)."""
    fails, errors = [], []
    for target, how, name in targets:
        refused, wrote = _attempt(project, settings, target, how)
        if wrote:
            fails.append(_("{path} was written", path=name))
            continue
        if not refused:
            errors.append(_("no refused attempt at {path}", path=name))
            continue
        also = _staged_at(target) if how == "stage" else []
        loose = _without(settings, target, also)
        if any(_covered(loose, t) for t in [target, *also]):
            errors.append(_("{path}'s rules can't be left out", path=name))
            continue
        if not _attempt(project, loose, target, how)[1]:
            errors.append(_("tests nothing: {path} wasn't written with its rules left out", path=name))
    return fails, errors


def _verdict(fails, errors, passed: str = "") -> Outcome:
    if fails:
        return Outcome("fail", "; ".join(fails + errors))
    if errors:
        return Outcome("error", "; ".join(errors))
    return Outcome("pass", passed)


SEEDED = "agent-memory"   # an entry present in the tool folder before the settings are rendered (its own denies)
WORKTREE = "wt-canary"   # a worktree present when the settings are rendered (a session folder of its own)


def _other_canaries(project: pathlib.Path, worktree: pathlib.Path) -> list:
    """Other agents' control folders (the policy's protect entries that aren't Claude's own, docs/design/runtime-codex.md,
    Step 4 r2, S1), which only the rendered rules keep a session off:
    - each at the top, through the file tools and Bash (sh -c), and nested, through Bash, which the sandbox must refuse
      from the Edit rules it takes in;
    - the first in a worktree made after the settings were rendered (so no second-layer entry names it; review S4);
    - the first built in the session's temp folder and moved into the project as one rename (review A-B2);
    - each name's long-s spelling (U+017F) at the top through the file tools, which the volume opens as the name and the
      rules match only literally (review A-C1)."""
    import policy
    own = {n for n, _t in rc.CONTROL}
    names = [c["name"] for c in policy.protect() if c["tree"] and c["name"] not in own and "/" not in c["name"]]
    out = [(project / n / "canary", how, f"{n}/canary ({how})") for n in names for how in ("file", "bash")]
    out += [(project / "sub" / n / "canary", "bash", f"sub/{n}/canary (bash)") for n in names]
    if names:
        tag = os.urandom(3).hex()
        out += [(worktree / names[0] / "canary", "bash", f"{rc.WORKTREES}/{WORKTREE}/{names[0]}/canary (bash)"),
                (project / f"moved-{tag}" / names[0] / "canary", "stage", f"$TMPDIR/…/{names[0]} moved in (bash)")]
    out += [(project / f / "canary", "file", f"{f}/canary (file)") for n in names for f in rc._folds(n)[1:]]
    return out


def control(world) -> Outcome:
    """A sandboxed project's session can't write what its sessions load settings, hooks and tools from. Through the
    rendered settings, a canary write is refused at each of these, and lands with that path's rules left out:
    - each control path in the project and in the user's tool folder;
    - inside an entry present in the project's tool folder, through the file tools and through Bash (Claude's own
      sandbox must refuse there);
    - at other agents' control folders (_other_canaries).
    A Bash canary counts as refused only when its result shows the sandbox's refusal. The new name no rule lists
    (section 7's residual) is recorded, never counted."""
    seeded = pathlib.Path(world.project) / rc.TOOL_FOLDER / SEEDED
    seeded.mkdir(parents=True, exist_ok=True)
    project, _neighbour, settings = _rendered(world)
    worktree = project / rc.WORKTREES / WORKTREE   # made after the settings were rendered: no entry names it
    worktree.mkdir(parents=True, exist_ok=True)
    user = rc._home()
    targets = [(project / c, "file", c) for c in CONTROL_CANARIES]
    targets += [(seeded / "canary", how, f"{rc.TOOL_FOLDER}/{SEEDED}/canary ({how})") for how in ("file", "bash")]
    targets += [(user / rel, "file", f"<user>/{rel}") for rel in ("settings.json", "remote/ccd-cli/canary")]
    targets += _other_canaries(project, worktree)
    fails, errors = _canaries(project, settings, targets)
    probe = project / rc.TOOL_FOLDER / f"probe-{os.urandom(3).hex()}"
    said = {}
    for how in ("file", "bash"):
        refused, wrote = _attempt(project, settings, probe, how)
        said[how] = _("written") if wrote else _("refused") if refused else _("not tried")
    residual = _("residual: a new {folder}/<name> is {file} through Edit, {bash} through Bash (sh -c)",
                 folder=rc.TOOL_FOLDER, file=said["file"], bash=said["bash"])
    return _verdict(fails, errors, residual)


def settings_taken(world) -> Outcome:
    """A sandboxed project's settings file with everything of the user's that may stand in it (runtime_claude.carried) is
    taken: the tool loads nothing of a file in which it doesn't take one value, the sandbox included, and says nothing.
    Told by a hook in the file that runs at the session's start, which no model call is needed for; a file with a value
    it doesn't take shows that the hook tells. Then by what the settings hold: another project's folder isn't written."""
    project, neighbour, _settings = _rendered(world)
    rps = _script("render-project-settings")
    f, mark = rc.settings_path(project), pathlib.Path(world.root) / f"settings-taken-{os.urandom(3).hex()}"
    f.parent.mkdir(exist_ok=True)
    odd = ["", "nonsense((", "bash(x)", "mcp__x(y)", "Bash(a:*b)", "\x00", "\U0001f600"]   # any text
    theirs = {**{k: "no-such" for k in rc.CARRIED_TEXT if k != "model"}, "model": FAST, **{k: odd for k in rc.CARRIED_LISTS},
              **{k: False for k in rc.CARRIED_SWITCHES}, "permissions": {k: odd for k in rc.CARRIED_RULES},
              "env": {"": "x", "A=B": "x", "A": "x\x00y"}}
    import registry
    index = registry.refresh()

    def rendered(mode: str | None) -> dict:
        f.write_text(json.dumps({**theirs, "permissions": {**theirs["permissions"], **({"defaultMode": mode} if mode else {})}}))
        return rps.compose(SANDBOXED, index["projects"][SANDBOXED], index)[2]

    def loaded(extra: dict, mode: str | None) -> bool | None:
        got = rendered(mode)
        mark.unlink(missing_ok=True)
        hooks = {**got["hooks"], "SessionStart": [*got["hooks"]["SessionStart"], {"hooks": [{"type": "command", "command": f"touch {mark}"}]}]}
        f.write_text(json.dumps({**got, "hooks": hooks, **extra}))   # the platform's own hooks too: they have to be taken
        r = _claude(["-p", "--tools", "", "--model", FAST, "--output-format", "json", "--strict-mcp-config"], cwd=project, stdin=ASK)
        return mark.exists() if mark.exists() or _result(r) else None   # it didn't even start: nothing can be said

    try:
        seen = {mode: loaded({}, mode) for mode in rc.CARRIED_MODES}
        control = loaded({"model": 5}, None)
        if None in seen.values() or control is None:
            return Outcome("error", _("the tool gave no result"))
        if not all(seen.values()):
            return Outcome("fail", _("not taken with the permission mode: {modes}", modes=", ".join(m for m, ok in seen.items() if not ok)))
        if control:
            return Outcome("fail", _("tests nothing: a file with a value it doesn't take ran its hook too"))
        got = rendered(None)
        got.pop("hooks")
        return _verdict(*_canaries(project, got, [(neighbour / f"canary-{os.urandom(3).hex()}", "file", _("another project's folder"))]))
    finally:
        f.unlink(missing_ok=True)
        mark.unlink(missing_ok=True)


# ---------- what the platform's own values stand in the place of (docs/design/capabilities.md, "A project's own settings
# files"): a committed settings file says the opposite of each, and a session's command or a hook shows which holds. Each
# has its control, the same without the platform's value, where the committed one does something; one that does nothing
# tests nothing. The model is a stand-in on this machine that asks for one command, with a key that is nobody's: no login,
# no network.

class _StandIn:
    """A model API on 127.0.0.1 that asks for these Bash commands, one a turn, then ends. keys: what it was called with."""

    def __init__(self):
        import http.server
        import threading
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, kind, data):
                self.send_response(200)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                outer.keys.add((self.headers.get("authorization"), self.headers.get("x-api-key")))
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
                except ValueError:
                    body = {}
                if "count_tokens" in self.path:
                    return self._send("application/json", b'{"input_tokens":1}')
                told = [c for m in body.get("messages") or [] if isinstance(m.get("content"), list) for c in m["content"] if c.get("type") == "tool_result"]
                outer.told = [json.dumps(c.get("content")) for c in told] or outer.told
                plan = outer.plan if body.get("tools") else []
                block, why = ({"type": "text", "text": ""}, "end_turn") if len(told) >= len(plan) else \
                    ({"type": "tool_use", "id": f"toolu_{os.urandom(8).hex()}", "name": "Bash", "input": {}}, "tool_use")
                delta = {"type": "text_delta", "text": "done"} if why == "end_turn" else \
                    {"type": "input_json_delta", "partial_json": json.dumps({"command": plan[len(told)], "description": "x"})}
                msg = {"id": f"msg_{os.urandom(8).hex()}", "type": "message", "role": "assistant", "model": body.get("model", "x"), "content": [],
                       "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
                events = [("message_start", {"message": msg}), ("content_block_start", {"index": 0, "content_block": block}),
                          ("content_block_delta", {"index": 0, "delta": delta}), ("content_block_stop", {"index": 0}),
                          ("message_delta", {"delta": {"stop_reason": why, "stop_sequence": None}, "usage": {"output_tokens": 1}}), ("message_stop", {})]
                if not body.get("stream"):
                    return self._send("application/json", json.dumps({**msg, "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}).encode())
                self._send("text/event-stream", "".join(f"event: {t}\ndata: {json.dumps({'type': t, **d})}\n\n" for t, d in events).encode())

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.plan, self.told, self.keys = [], [], set()

    def run(self, cwd, commands, key=True) -> str | None:
        """What the session's commands gave, or None when the tool gave no result."""
        self.plan, self.told = list(commands), []
        env = {k: v for k, v in os.environ.items() if k not in (rc.TOKEN_VAR, *rc.METERED_VARS)}
        env |= {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.server.server_port}", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1"}
        env |= {"ANTHROPIC_API_KEY": "sk-ant-nobodys"} if key else {rc.TOKEN_VAR: NOBODYS}
        r = _claude(["-p", "--output-format", "json", "--strict-mcp-config", "--setting-sources", "project,local", "--model", FAST], cwd=cwd, stdin=ASK,
                    timeout=STAND_IN_S, env=env)
        got = _result(r)
        return None if got is None or got.get("is_error") else "\n".join(self.told)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


STAND_IN_S = 120
NOBODYS = "sk-ant-oat01-nobodys"


def _neutral_cases(root: pathlib.Path, sb: dict, mark: pathlib.Path, sock: str) -> list[tuple]:
    """(what, the committed file, the local file with the platform's value, the same without it, the command, whether the
    committed value held)."""
    allow, secret = {"permissions": {"allow": ["Bash(*)"]}}, root / "neutral-secret"
    secret.mkdir(exist_ok=True)
    (secret / "x").write_text("S3CRET")

    def program(name, text):
        (root / name).write_text(text)
        (root / name).chmod(0o700)
        return str(root / name)

    def without(d, *path):
        out = json.loads(json.dumps(d))
        at = out
        for k in path[:-1]:
            at = at[k]
        at.pop(path[-1], None)
        return out

    def hook(command):
        return {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]}}
    marked = lambda out: mark.exists()   # noqa: E731
    ours = hook(rc.hook_command("prompt"))
    sh = program("neutral-sh", f"/usr/bin/touch {mark}\n")
    prefix = program("neutral-prefix", f'#!/bin/sh\n/usr/bin/touch {mark}\nexec /bin/bash -c "$1"\n')
    python = program("neutral-python", f"#!/bin/sh\n/usr/bin/touch {mark}\n")
    planted = root / "neutral-planted"
    planted.mkdir(exist_ok=True)
    (planted / "runpy.py").write_text(f"open({str(mark)!r}, 'w')\n")
    unread = {**sb, "credentials": {"files": [{"path": str(secret), "mode": "deny"}]}, "filesystem": {**sb["filesystem"], "denyRead": [str(secret)]}}
    knock = f"{sys.executable} -c 'import socket;s=socket.socket(socket.AF_UNIX);s.connect(\"{sock}\");print(\"REACH\"+\"ED\")'"
    interpreter = names.env_names("PYTHON")[1]
    steer = {"PYTHONPATH": str(planted), **dict.fromkeys(names.env_names("PYTHON"), python), **dict.fromkeys(names.env_names("ROOT"), str(root))}
    return [
        (_("every hook off"), {"disableAllHooks": True}, {**hook(f"touch {mark}.hook"), "disableAllHooks": False}, hook(f"touch {mark}.hook"), "true",
         lambda out: not pathlib.Path(f"{mark}.hook").exists()),
        (_("sockets"), {"sandbox": {"network": {"allowAllUnixSockets": True}}}, {"sandbox": sb, **allow},
         {"sandbox": without(sb, "network", "allowAllUnixSockets"), **allow}, knock, lambda out: "REACHED" in out),
        (_("commands without asking"), {"sandbox": {"autoAllowBashIfSandboxed": True}}, {"sandbox": sb}, {"sandbox": without(sb, "autoAllowBashIfSandboxed")},
         "sh -c 'echo A\"S\"KED'", lambda out: "ASKED" in out),
        (_("reading what is denied"), {"sandbox": {"filesystem": {"allowRead": [str(secret)]}}}, {"sandbox": unread, **allow},
         {"sandbox": without(unread, "credentials"), **allow}, f"cd {root}; cat neutral-s''ecret/x", lambda out: "S3CRET" in out),
        (_("the key helper"), {"apiKeyHelper": f"touch {mark}; echo sk-x"}, {"sandbox": sb, "apiKeyHelper": "", **allow}, {"sandbox": sb, **allow}, "true", marked),
        ("BASH_ENV", {"env": {"BASH_ENV": sh, "ENV": sh}}, {"sandbox": sb, "env": rc.neutral_env(), **hook("bash -c true"), **allow},
         {"sandbox": sb, **hook("bash -c true"), **allow}, "true", marked),
        ("CLAUDE_CODE_SHELL_PREFIX", {"env": {"CLAUDE_CODE_SHELL_PREFIX": prefix}}, {"sandbox": sb, "env": rc.neutral_env(), **ours, **allow},
         {"sandbox": sb, **ours, **allow}, "true", marked),
        (_("the hooks' environment"), {"env": steer}, {"sandbox": sb, **ours, **allow},
         {"sandbox": sb, **hook(f'"${interpreter}"; {sys.executable} -c "import runpy"'), **allow}, "true", marked),
    ]


def session_neutral(world) -> Outcome:
    """Each value the platform renders so that a committed one of the opposite doesn't hold (the switches, the places no file
    lets read, the variables, the key helper), and the environment its hooks have. And the empty key helper beside a
    login: the login's token is what is sent, as without it."""
    import socket
    project, _neighbour, settings = _rendered(world)
    root, sb = pathlib.Path(world.root), settings["sandbox"]
    shared, local = project / ".claude" / "settings.json", rc.settings_path(project)
    local.parent.mkdir(exist_ok=True)
    mark = root / "neutral-mark"
    sock = f"/tmp/cc-{os.urandom(4).hex()}.sock"   # a socket's path has a length of its own
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(sock)
    srv.listen(5)
    api, fails, errors, untold = _StandIn(), [], [], []
    try:
        for what, theirs, mine, control, command, held in _neutral_cases(root, sb, mark, sock):
            shared.write_text(json.dumps(theirs))
            for doc, rendered in ((mine, True), (control, False)):
                local.write_text(json.dumps(doc))
                for f in (mark, pathlib.Path(f"{mark}.hook")):
                    f.unlink(missing_ok=True)
                out = api.run(project, [command])
                if out is None:
                    errors.append(_("{what}: the tool gave no result", what=what))
                elif rendered and held(out):
                    fails.append(_("a committed settings file's value held: {what}", what=what))
                elif not rendered and not held(out):   # measured on macOS; another OS's sandbox may have no such thing to widen
                    (errors if sys.platform == "darwin" else untold).append(_("tests nothing: {what} did nothing without the platform's value", what=what))
                else:
                    continue
                break
        shared.unlink(missing_ok=True)
        local.write_text(json.dumps({"sandbox": sb, "apiKeyHelper": ""}))
        api.keys.clear()
        if api.run(project, ["true"], key=False) is None:
            errors.append(_("{what}: the tool gave no result", what=_("the key helper")))
        elif api.keys != {(f"Bearer {NOBODYS}", None)}:
            fails.append(_("an empty key helper changes what a login sends"))
    finally:
        api.close()
        srv.close()
        for f in (pathlib.Path(sock), shared, local):
            f.unlink(missing_ok=True)
    return _verdict(fails, errors, "; ".join(untold))


def nested_settings(world) -> Outcome:
    """A session opened in a repo in the project's folder reads that folder's settings file, one with an empty .git included;
    and a live session goes on with its sandbox when its settings file is replaced and the platform's check refuses (its
    exit 2)."""
    project, _neighbour, settings = _rendered(world)
    api, outside = _StandIn(), pathlib.Path(world.home) / f"nested-{os.urandom(3).hex()}"
    sb, allow = settings["sandbox"], {"permissions": {"allow": ["Bash(*)"]}}
    fails, errors = [], []
    try:
        for kind in ("file", "folder", "repo"):
            d = project / f"nested-{kind}"
            (d / ".claude").mkdir(parents=True, exist_ok=True)
            if kind == "repo":
                subprocess.run(["git", "init", "-q", str(d)], capture_output=True, timeout=60)
            elif kind == "file":
                (d / ".git").write_text("")
            else:
                (d / ".git").mkdir(exist_ok=True)
            for with_it in (True, False):
                rc.settings_path(d).write_text(json.dumps({"sandbox": sb, **allow} if with_it else allow))
                outside.unlink(missing_ok=True)
                if api.run(d, [f"echo x > {outside}"]) is None:
                    errors.append(_("{what}: the tool gave no result", what=f"nested-{kind}"))
                elif with_it and outside.exists():
                    fails.append(_("the settings file of a repo in the project's folder isn't read ({what})", what=f"nested-{kind}"))
        f = rc.settings_path(project)
        f.parent.mkdir(exist_ok=True)
        change = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"echo '{json.dumps(allow)}' > {f}"}]}]}
        refuse = {rc.CHANGE_HOOK: [{"matcher": rc.CHANGE_SOURCE, "hooks": [{"type": "command", "command": "exit 2"}]}]}
        for guarded in (True, False):
            f.write_text(json.dumps({"sandbox": sb, **allow, "hooks": {**change, **(refuse if guarded else {})}}))
            outside.unlink(missing_ok=True)
            if api.run(project, ["true", "sleep 5", "sleep 3", "sleep 1", f"echo x > {outside}"]) is None:
                errors.append(_("{what}: the tool gave no result", what=rc.CHANGE_HOOK))
            elif guarded and outside.exists():
                fails.append(_("a live session takes in a settings file its check refuses"))
            elif not guarded and not outside.exists():
                errors.append(_("tests nothing: a settings file replaced under a live session changed nothing"))
    finally:
        api.close()
        outside.unlink(missing_ok=True)
    return _verdict(fails, errors)


def confine_sandbox(world) -> Outcome:
    """A sandboxed project's session writes neither another project's folder nor the platform's secrets, through the
    rendered settings; with those rules left out it does (each canary its own positive control)."""
    project, neighbour, settings = _rendered(world)
    import hubconf
    tag = os.urandom(3).hex()
    return _verdict(*_canaries(project, settings, [
        (neighbour / f"canary-{tag}", "file", _("another project's folder")),
        (hubconf.secrets_dir() / f"canary-{tag}", "file", _("the platform's secrets"))]))


def session_write(world) -> Outcome:
    """A sandboxed project's session writes its own push request folder through the rendered settings (policy
    session_write; docs/design/platform-push.md, "Who may request"), and not another project's: that canary is refused,
    and lands with its rules left out."""
    import policy
    project, _neighbour, settings = _rendered(world)
    own, other = policy.push_request_dir(SANDBOXED), policy.push_request_dir(NEIGHBOUR)
    for d in (own, other):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    tag = os.urandom(3).hex()
    if not _attempt(project, settings, own / f"canary-{tag}", "bash")[1]:
        return Outcome("fail", _("its own push request folder wasn't writable"))
    # Another project's folder is kept off by two things: it isn't among this session's writable places, and the
    # rendered denies name it. Its positive control must lift both (written there as if it were the session's own
    # folder, its denies left out), or it can never land and the check tests nothing (11-18: every version was held for
    # it). _canaries' control only leaves denies out, which is enough where the place is writable anyway (a temp root).
    target, name = other / f"canary-{tag}", _("another project's push request folder")
    refused, wrote = _attempt(project, settings, target, "bash")
    if wrote:
        return Outcome("fail", _("{path} was written", path=name))
    if not refused:
        return Outcome("error", _("no refused attempt at {path}", path=name))
    loose = _without(settings, target)
    fs = loose.setdefault("sandbox", {}).setdefault("filesystem", {})
    fs["allowWrite"] = [*fs.get("allowWrite", []), str(other)]
    if not _attempt(project, loose, target, "bash")[1]:
        return Outcome("error", _("tests nothing: {path} wasn't written with its rules left out", path=name))
    return Outcome("pass", "")


# ---------- plugins in the platform's own runs (docs/design/agent-surface.md, "Platform runs"; review S6) ----------
# Claude Code loads a plugin from a skills folder (the project's .claude/skills/<x>, the config dir's skills/<x>) and the
# ones installed from a marketplace, and their hooks run as the user, outside every sandbox. Measured 11-18 on 2.1.327: in
# a plain `claude -p` all three load (the project's only once its folder is trusted), both a hooks list and a hooks
# module; with --restricted none does; --plugin-dir loads either way (the platform never passes it); a config dir's
# skills are read from it alone (~/.claude/skills isn't, when CLAUDE_CONFIG_DIR names another).
PLUGIN_PLACES = ("project-skills", "user-skills", "marketplace")


def _canary_plugin(dest: pathlib.Path, name: str, marker: pathlib.Path, module: bool) -> None:
    """A plugin that writes marker when a session starts: through a hooks list's command, or a hooks module's
    session.start hook (its two ways to run code)."""
    shutil.rmtree(dest, ignore_errors=True)
    (dest / ".claude-plugin").mkdir(parents=True)
    (dest / "hooks").mkdir()
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": "0.1.0",
                                                                      "description": "conformance canary"}))
    if module:
        (dest / "hooks" / "hooks.json").write_text(json.dumps({"modules": ["./register.ts"]}))
        (dest / "hooks" / "register.ts").write_text(
            "import type { Register } from 'claude-code'\nexport const register: Register = (on) => {\n"
            f"  on('session.start', async ($, e, next) => {{ await $.fs.write({json.dumps(str(marker))}, 'ran'); "
            "return next(e) })\n}\n")
    else:
        (dest / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": f"echo ran >> '{marker}'"}]}]}}))


def _plant_plugins(project: pathlib.Path, config: pathlib.Path, trust: pathlib.Path, marks: pathlib.Path, kind: str,
                   env: dict) -> tuple[dict[str, pathlib.Path], str | None]:
    """Canaries of both forms in each PLUGIN_PLACES for one run kind (config: its config dir; trust: the global config file
    that records the project folder as trusted, as a person's would). ({place/form: marker}, why it couldn't be done)."""
    marks.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    try:
        doc = json.loads(trust.read_text()) if trust.is_file() else {}
    except ValueError:
        doc = {}
    doc.setdefault("projects", {})[str(project.resolve())] = {"hasTrustDialogAccepted": True}
    trust.write_text(json.dumps(doc))
    out = {}
    for module in (False, True):
        form = "module" if module else "hooks"
        for place in PLUGIN_PLACES:
            name = f"canary-{kind}-{place}-{form}"
            marker = marks / name
            marker.unlink(missing_ok=True)
            out[f"{place}/{form}"] = marker
            if place == "project-skills":
                _canary_plugin(project / ".claude" / "skills" / name, name, marker, module)
            elif place == "user-skills":
                _canary_plugin(config / "skills" / name, name, marker, module)
            else:
                market = marks.parent / f"market-{name}"
                _canary_plugin(market / "plugin", name, marker, module)
                (market / ".claude-plugin").mkdir(exist_ok=True)
                (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
                    {"name": f"m-{name}", "owner": {"name": "conformance"},
                     "plugins": [{"name": name, "source": "./plugin", "description": "conformance canary"}]}))
                for args in (["plugin", "marketplace", "add", str(market)], ["plugin", "install", f"{name}@m-{name}"]):
                    r = _claude(args, cwd=project, env=env, timeout=180)
                    if r.returncode != 0:
                        return out, _("installing the canary from a marketplace failed (exit {code})", code=r.returncode)
    return out, None


def _ran(markers: dict[str, pathlib.Path]) -> list[str]:
    return sorted(k for k, m in markers.items() if m.exists())


def platform_run_plugins(world) -> Outcome:
    """No plugin of the folder's or the user's loads in a platform run: canaries in the project's skills folder, the
    config dir's skills folder and installed from a marketplace (and for the own kind the home's ~/.claude/skills), for
    both run kinds with their own argv (a completion:
    the platform's config dir; a restricted agent point: the user's, as `shared`). The control is the same run without
    --restricted, which must load every one (else that placement tests nothing). A model call each."""
    import llm
    from runtime_ports import Task
    exe = rc.binary()
    project, tmp = pathlib.Path(world.project), pathlib.Path(world.tmp)
    shared_env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    home = pathlib.Path(shared_env.get("HOME") or world.home)
    spec = llm.points()["points"]["mailbox-propose"]   # an agent point with restricted: true
    kinds = (
        ("own", dict(os.environ), pathlib.Path(world.tool), pathlib.Path(world.tool) / ".claude.json",
         _completion_argv()),
        ("shared", shared_env, home / ".claude", home / ".claude.json",
         rc._command(Task(point="mailbox-propose", prompt=ASK, spec=spec, cwd=str(project), model=FAST,
                          env=dict(shared_env)), exe)[1:]),
    )
    fails, errors = [], []
    for kind, env, config, trust, argv in kinds:
        if "--restricted" not in argv:
            fails.append(_("the {kind} run's argv has no --restricted", kind=kind))
            continue
        markers, why = _plant_plugins(project, config, trust, tmp / f"plugins-{kind}" / "marks", kind, env)
        if why:
            errors.append(f"{kind}: {why}")
            continue
        # the own kind's config dir isn't the home's: canaries in the home's ~/.claude/skills too, which no run of that
        # kind reads even without --restricted (measured 11-18), so they have no control and count against the guarded
        # run only
        extra = {}
        if kind == "own":
            for module in (False, True):
                form = "module" if module else "hooks"
                name = f"canary-own-home-skills-{form}"
                extra[f"home-skills/{form}"] = tmp / f"plugins-{kind}" / "marks" / name
                _canary_plugin(home / ".claude" / "skills" / name, name, extra[f"home-skills/{form}"], module)
        loose = [a for a in argv if a != "--restricted"]
        for name, args in (("control", loose), ("guarded", argv)):
            for m in [*markers.values(), *extra.values()]:
                m.unlink(missing_ok=True)
            r = _claude(args, cwd=project, stdin=ASK, env=env, timeout=RUN_S)
            got = _result(r) if "stream-json" not in args else next(
                (o for o in reversed(_events(r)) if o.get("type") == "result"), None)
            if got is None or got.get("is_error"):
                errors.append(_("{kind} {run} run: {why}", kind=kind, run=name, why=_fail_reason(r, got)))
                break
            ran = _ran(markers)
            if name == "control" and (missed := sorted(set(markers) - set(ran))):
                errors.append(_("tests nothing: {places} didn't load in a {kind} run without --restricted either",
                                places=", ".join(missed), kind=kind))
                break
            if name == "guarded" and (ran := _ran({**markers, **extra})):
                fails.append(_("a platform run ({kind}) loaded these plugins, whose hooks run as you: {places}",
                               kind=kind, places=", ".join(ran)))
        shutil.rmtree(project / ".claude" / "skills", ignore_errors=True)
        for d in extra:
            shutil.rmtree(home / ".claude" / "skills" / f"canary-own-{d.replace('/', '-')}", ignore_errors=True)
    return _verdict(fails, errors, _("no plugin loaded in either run kind; each loaded without --restricted"))


V_M6 = "a check through V-M6's pty driver (an interactive throwaway session; measured 11-09, not built as a check yet)"
CHECKS = [
    Check("distribution-integrity", "distribution", "function", distribution, timeout_s=120),
    Check("agent-result", "agent", "function", agent_result),
    Check("agent-models", "agent", "function", agent_models, timeout_s=1000),
    Check("completion-schemas", "completion", "function", completion_schemas, timeout_s=3000),
    Check("completion-no-tools", "completion", "safety", completion_no_tools),
    Check("confine-folder-git", "confine", "safety", confine_folder_git, timeout_s=900),
    # canaries in three places x two forms, per run kind: 2 marketplace installs (2 commands each) + control + guarded
    Check("platform-run-plugins", "confine", "safety", platform_run_plugins, timeout_s=2 * (4 * 180 + 2 * RUN_S) + 120),
    # each run up to RUN_S, asked twice when it shows nothing: confine-sandbox 2 canaries x (guarded + control),
    # control 9 canaries + 9 of other agents' control folders (2 each at the top, 1 each nested, 1 in a worktree, 1
    # staged and moved in, 1 long-s spelling), x 2, + the residual's 2
    Check("settings-taken", "confine", "safety", settings_taken, timeout_s=(len(rc.CARRIED_MODES) + 1) * 300 + 2 * 2 * RUN_S + 60, side="session"),
    # no model: eight values each with its control, and the key helper beside a login; three folders with and without, and a
    # file replaced under a session
    Check("session-neutral", "confine", "safety", session_neutral, timeout_s=17 * STAND_IN_S + 60, side="session"),
    Check("nested-settings", "confine", "safety", nested_settings, timeout_s=8 * STAND_IN_S + 60, side="session"),
    Check("confine-sandbox", "confine", "safety", confine_sandbox, timeout_s=2 * 2 * 2 * RUN_S + 120, side="session"),
    Check("control", "control", "safety", control, timeout_s=(18 * 2 + 2) * 2 * RUN_S + 120, side="session"),
    # its own request folder written, the neighbour's refused and its positive control
    Check("session-write", "session_write", "safety", session_write, timeout_s=3 * 2 * RUN_S + 120, side="session"),
    Check("lifecycle-events", "lifecycle", "function", lifecycle_events, side="session"),
    Check("lifecycle-deny", "lifecycle", "safety", None,
          "a PreToolUse deny stops the tool: the one tool the platform denies (opening a session elsewhere) exists "
          "only in the desktop app, so it needs V-M6's interactive throwaway session with that tool", side="session"),
    Check("lifecycle-context", "lifecycle", "function", None,
          "additionalContext from a hook reaches the model: needs a letter in the world's bus for the hook to inject",
          side="session"),
    Check("instructions-reach", "instructions", "function", instructions_reach, side="session"),
    Check("sessions-read", "sessions", "function", sessions_read, side="session"),
    # a session of this version forked by the platform's current copy (a mailbox fork); on a candidate, by itself
    Check("sessions-fork", "sessions", "function", sessions_fork, side="session"),
    Check("stream-exchange", "stream", "function", stream_exchange),
    # a lease's --settings ask for every session command the folder's local settings allow (runtime_claude.lease_settings;
    # docs/design/platform-push.md, "After the code review"): ask must win over that allow
    Check("lease-ask", "stream", "safety", None,
          "a lease started in a folder whose local settings allow `brunna send *`, with --settings asking for it, sends "
          "the command to the permission prompt tool instead of running it: needs a lease session with a stand-in relay"),
    Check("tool-servers-listed", "tool_servers", "function", tool_servers_listed),
    Check("auth-login-error", "auth", "function", auth_login_error),
    Check("auth-keychain", "auth", "safety", auth_keychain),
    Check("rule-check", "rule_check", "safety", None,
          "each rule form the platform writes, probed against the tool's own parser"),
    Check("run-server", "run_server", "function", None,
          "a run with the platform's mail server attached lists its tools in the init event"),
    Check("credential-bridge", "credential_tools", "safety", credential_bridge, timeout_s=180),
    Check("session-status", "session_status", "function", None, V_M6, side="session"),
    Check("session-summary", "session_summary", "function", None, V_M6, side="session"),
    Check("wake", "wake", "function", None, V_M6, side="session"),
    # docs/design/git-broker.md r3 B-1: with the hook in a sandboxed project's settings, the tool's worktree mode runs it
    # and makes no worktree of its own (as the user)
    Check("worktree-maker", "worktree_maker", "safety", None,
          "worktree mode (--worktree, the EnterWorktree tool, a worktree subagent) in a folder whose settings hold the "
          "platform's WorktreeCreate hook runs the hook and its git makes no worktree as the user: needs V-M6's "
          "interactive throwaway session", side="session"),
]
