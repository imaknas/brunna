#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""Platform credentials: the platform's own grants for the remote MCP servers its unattended jobs use
(docs/design/platform-credentials.md). A registry secrets: entry with kind (oauth | static) declares one; a job lists
it in credentials:; the run gets it only through the platform's bridge (bin/credential_bridge.py), never in its
environment, arguments or files.

    brunna credentials status [<project>]          each credential's state (never a value)
    brunna credentials grant <project> <name>      the owner signs in, in a terminal on the hub machine (C2)
    brunna credentials set <project> <name>        a static value, read without echo
    brunna credentials revoke <project> <name>     revoke at the provider (RFC 7009) and delete it here
    brunna credentials check-published             the shared client's tripwire (route a; C2a)
    credentials.py keepalive                       the platform job brunna/credentials (hourly): owner to-dos for
                                                   what waits for the owner, refresh tokens kept alive, statuses
    credentials.py refresh <project> <name> [--failed <sha256>]   one refresh (the bridge starts it, detached)
    credentials.py rename <old> <new> [--apply]    rename-project.sh: the credentials follow the project

Where things are:
  <secrets>/credentials/<project>/<name>.json        the credential (600; not in the cloud backup: backup: false)
  <secrets>/credentials-kept/<project>/<name>.json   the same for an entry with backup: true (backed up)
  <secrets>/credentials*/<project>/<name>.lock        held by whoever refreshes or revokes it
  <state>/credentials/<project>/<name>.json          its status, no secret in it: check-secrets, doctor and the status
                                                     page read only this
A refresh never re-discovers: it uses the token endpoint recorded at the grant, and sends resource every time. A
grant that breaks (invalid_grant, invalid_client, a refresh whose answer was lost twice) needs the owner, never a
fallback: an alert, and by default (broken: todo) an owner to-do with the exact command, one per break (C10).
"""
from __future__ import annotations

import base64
import datetime
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402
import i18n  # noqa: E402
import names  # noqa: E402

_ = i18n.domain(__file__)
_c = i18n.domain(__file__, content=True)

ROOT = BIN.parent
OAUTH = ROOT / "oauth"
CALLBACK_PORT = 47615          # the shared client's fixed redirect port (C2 step 5); the registry reserves it
NEED_MAX_S = 3600              # fresh() never asks for more than an hour of lifetime
KEEPALIVE_S = 23 * 3600        # a refresh token unused this long is used (providers expire unused ones)
HTTP_TIMEOUT_S = 30
REQUIRE_HTTPS = True           # the metadata and endpoints a grant follows (tests serve them over local http)
# what makes an HTTP client go elsewhere than where it is told: never honoured here or in the bridge (C5)
ENV_DROP = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
SECURITY_FIELDS = ("client_id", "redirect_uris", "client_name", "grant_types", "token_endpoint_auth_method",
                   "token_endpoint_auth_methods_supported")


class NeedsGrant(Exception):
    """The grant can't be used until the owner grants it again."""


class Transient(Exception):
    """Try again later (a network error, a 503)."""


# ---------- where ----------
def bases() -> tuple[pathlib.Path, pathlib.Path]:
    s = hubconf.secrets_dir()
    return s / "credentials", s / "credentials-kept"


def base_for(sec: dict) -> pathlib.Path:
    return bases()[1] if sec.get("backup") is True else bases()[0]


def cred_path(project: str, name: str, sec: dict) -> pathlib.Path:
    return base_for(sec) / project / f"{name}.json"


def status_path(project: str, name: str) -> pathlib.Path:
    return hubconf.STATE / "credentials" / project / f"{name}.json"


def found(project: str, name: str) -> pathlib.Path | None:
    """Where the credential is, in either folder (a changed backup: setting leaves it in the other until keepalive moves
    it)."""
    for b in bases():
        p = b / project / f"{name}.json"
        if p.is_file() and not p.is_symlink():
            return p
    return None


# ---------- the registry ----------
def index() -> dict:
    import registry
    return registry.refresh()


def entries(doc: dict) -> dict:
    import registry
    return registry.credential_entries(doc)


def entry(project: str, name: str, idx: dict | None = None) -> tuple[dict, dict]:
    doc = (idx or index())["projects"].get(project)
    if doc is None:
        raise ValueError(_("{project} is not in the registry", project=project))
    sec = entries(doc).get(name)
    if sec is None:
        raise ValueError(_("{project} declares no platform credential {name} (secrets: with kind)", project=project,
                           name=name))
    return doc, sec


def jobs_using(doc: dict, name: str) -> list[dict]:
    return [j for j in doc.get("jobs") or [] if isinstance(j, dict) and name in (j.get("credentials") or [])]


# ---------- files ----------
def _read_private(path: pathlib.Path) -> dict:
    """A credential file: this user's own regular file that only it can read, read once without following a link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError(_("{file} is not a file of this user that only it can read", file=path))
        raw = b""
        while chunk := os.read(fd, 65536):
            raw += chunk
    finally:
        os.close(fd)
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError(_("{file} is not a JSON object", file=path))
    return d


def _write_private(path: pathlib.Path, data: dict) -> None:
    """Whole or not at all: a temporary file, fsynced, renamed over the old one, the folder fsynced."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for p in (path.parent.parent, path.parent):
        if p.exists():
            os.chmod(p, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(data, ensure_ascii=False, indent=1).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load(project: str, name: str) -> dict | None:
    p = found(project, name)
    return _read_private(p) if p else None


def read_status(project: str, name: str) -> dict:
    try:
        d = json.loads(status_path(project, name).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(project: str, name: str, st: dict) -> None:
    p = status_path(project, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def token_hash(token: str | None) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


class Lock:
    """flock on <name>.lock beside the credential: one refresher or revoker at a time."""

    def __init__(self, project: str, name: str, sec: dict):
        self.path = base_for(sec) / project / f"{name}.lock"

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


# ---------- HTTP: no proxy, no redirect, no CA from the environment ----------
def scrub_env(env=None) -> None:
    for k in ENV_DROP:
        (os.environ if env is None else env).pop(k, None)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error: the standard library would re-send the Authorization header to wherever it points (measured
    in the r4 review: a 302 on a POST became a GET to another origin, the header with it)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener():
    import ssl
    scrub_env()
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                       urllib.request.HTTPSHandler(context=ctx))


def http(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None, op=None):
    """→ (status, headers, body). A network error raises Transient; 'lost' is set on it when the request may have been
    received (a timeout or a reset after sending)."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": f"{names.current('cli')}-credentials", **(headers or {})})
    try:
        r = (op or opener()).open(req, timeout=HTTP_TIMEOUT_S)
        return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except (TimeoutError, ConnectionResetError) as e:
        err = Transient(str(e))
        err.lost = True
        raise err
    except (urllib.error.URLError, OSError) as e:
        lost = isinstance(getattr(e, "reason", None), (TimeoutError, ConnectionResetError))
        err = Transient(str(getattr(e, "reason", e)))
        err.lost = lost
        raise err


def get_json(url: str, op=None) -> dict | None:
    status, _h, body = http("GET", url, headers={"Accept": "application/json"}, op=op)
    if status != 200:
        return None
    try:
        d = json.loads(body)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def client_auth(client: dict, form: dict) -> dict:
    """The token request's client authentication (the grant's recorded method): headers, and the form changed in place."""
    method = client.get("auth_method") or "none"
    form["client_id"] = client["client_id"]
    if method == "client_secret_basic":
        form.pop("client_id", None)
        pair = f"{urllib.parse.quote(client['client_id'], safe='')}:{urllib.parse.quote(client.get('client_secret') or '', safe='')}"
        return {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}
    if method == "client_secret_post":
        form["client_secret"] = client.get("client_secret") or ""
    return {}


def token_request(cred: dict, form: dict, op=None) -> tuple[int, dict]:
    """POST to the recorded token endpoint, resource always sent (C2 step 6)."""
    form = {**form, "resource": cred["resource"]}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
               **client_auth(cred["client"], form)}
    status, _h, body = http("POST", cred["token_endpoint"], data=urllib.parse.urlencode(form).encode(), headers=headers,
                            op=op)
    try:
        d = json.loads(body) if body else {}
    except ValueError:
        d = {}
    return status, d if isinstance(d, dict) else {}


def apply_tokens(cred: dict, answer: dict) -> dict:
    out = {**cred, "access_token": answer["access_token"], "issued_at": time.time()}
    exp = answer.get("expires_in")
    out["access_expires_at"] = time.time() + float(exp) if isinstance(exp, (int, float)) and exp > 0 else None
    if answer.get("refresh_token"):
        out["refresh_token"] = answer["refresh_token"]
    if answer.get("scope"):
        out["scope"] = answer["scope"]
    return out


# ---------- refresh ----------
def _take_pending(path: pathlib.Path, cred: dict | None) -> dict | None:
    """A .new a refresher left between the provider's answer and the rename: taken when it parses as a whole credential
    and is newer than the file (its refresh token or access token differs and it was issued later), else dropped."""
    new = path.with_name(path.name + ".new")
    if not new.exists():
        return cred
    try:
        cand = _read_private(new)
        newer = cred is None or (cand.get("issued_at", 0) > (cred.get("issued_at") or 0)
                                 and (cand.get("refresh_token"), cand.get("access_token"))
                                 != (cred.get("refresh_token"), cred.get("access_token")))
        if cand.get("kind") == "oauth" and cand.get("access_token") and newer:
            os.replace(new, path)
            return cand
    except (OSError, ValueError):
        pass
    new.unlink(missing_ok=True)
    return cred


def _commit(path: pathlib.Path, cred: dict) -> None:
    new = path.with_name(path.name + ".new")
    _write_private(new, cred)
    os.replace(new, path)


def refresh(project: str, name: str, failed: str | None = None, idx: dict | None = None, op=None) -> str:
    """One refresh, under the lock; → the access token now stored. failed: the hash of the token a request was refused
    with; when the stored one is already another, someone refreshed meanwhile and nothing is sent."""
    doc, sec = entry(project, name, idx)
    with Lock(project, name, sec):
        path = found(project, name) or cred_path(project, name, sec)
        cred = _take_pending(path, load(project, name))
        if cred is None:
            raise NeedsGrant(_("not granted yet"))
        if cred.get("kind") == "static":
            if failed and token_hash(cred.get("value")) == failed:   # the server refused the value itself
                raise _broken(project, name, sec, _("the server refused the static value"))
            return cred["value"]
        if failed and token_hash(cred.get("access_token")) != failed:
            return cred["access_token"]
        if not cred.get("refresh_token"):
            raise _broken(project, name, sec, _("the access token expired and the grant has no refresh token"))
        form = {"grant_type": "refresh_token", "refresh_token": cred["refresh_token"]}
        for attempt in (1, 2):
            try:
                status, answer = token_request(cred, form, op=op)
                break
            except Transient as e:
                if not getattr(e, "lost", False):
                    _note(project, name, last_result="transient", last_error=str(e))
                    raise
                if attempt == 2:   # a rotated refresh token may be gone with the lost answer: never a loop
                    raise _broken(project, name, sec, _("the answer to a refresh was lost twice ({why})", why=e))
        if status == 200 and answer.get("access_token"):
            cred = apply_tokens(cred, answer)
            _commit(path, cred)
            _note(project, name, state="granted", last_refresh=now_iso(), last_result="ok", last_error=None,
                  refresh_used_at=now_iso(), access_expires_at=cred.get("access_expires_at"))
            return cred["access_token"]
        err = answer.get("error")
        if err == "invalid_client":
            raise _broken(project, name, sec, _("the provider no longer accepts this grant's client ({client}): "
                                                "invalid_client", client=cred["client"].get("client_id")))
        if err in ("invalid_grant", "unauthorized_client") or status in (400, 401):
            raise _broken(project, name, sec, _("the provider refused the refresh ({error})", error=err or status))
        _note(project, name, last_result="transient", last_error=f"HTTP {status}")
        raise Transient(_("the token endpoint answered HTTP {status}", status=status))


def fresh(project: str, name: str, need_s: int = 600, idx: dict | None = None, op=None) -> str:
    """The access token, with at least need_s (capped at an hour) of its lifetime left: the stored one, else one refresh,
    whose token is taken whatever its lifetime (a provider with short-lived tokens never loops). NeedsGrant / Transient."""
    doc, sec = entry(project, name, idx)
    cred = load(project, name)
    if cred is None:
        raise NeedsGrant(_("{project}/{name} is not granted yet: run {command}", project=project, name=name,
                           command=grant_command(project, name, sec)))
    if read_status(project, name).get("state") == "needs_grant":
        raise NeedsGrant(_("{project}/{name} needs the owner: run {command}", project=project, name=name,
                           command=grant_command(project, name, sec)))
    if cred.get("kind") == "static":
        return cred["value"]
    exp = cred.get("access_expires_at")
    if exp is None or exp - time.time() > min(need_s, NEED_MAX_S):
        return cred["access_token"]
    return refresh(project, name, idx=idx, op=op)


def grant_command(project: str, name: str, sec: dict) -> str:
    sub = "set" if sec.get("kind") == "static" else "grant"
    return f"{names.current('cli')} credentials {sub} {project} {name}"


def _note(project: str, name: str, **fields) -> dict:
    st = read_status(project, name)
    st.update(fields)
    st["updated_at"] = now_iso()
    write_status(project, name, st)
    return st


def _broken(project: str, name: str, sec: dict, why: str) -> NeedsGrant:
    """A grant that needs the owner: the status says so, a new episode starts when it was working, an alert and (broken:
    todo) the owner to-do. → the exception to raise."""
    st = read_status(project, name)
    if st.get("state") == "granted":
        st["episode"] = int(st.get("episode") or 1) + 1
    st.update(state="needs_grant", last_result="broken", last_error=why, updated_at=now_iso())
    write_status(project, name, st)
    raise_todo(project, name, sec, why)
    text = _("{project}/{name}: {why}. The owner re-grants it with {command}", project=project, name=name, why=why,
             command=grant_command(project, name, sec))
    try:
        import alert
        alert.once(f"credential:{project}:{name}", [text], _("{project}/{name} works again", project=project, name=name))
    except Exception as e:   # a broken alert doesn't change the outcome
        print(_("credentials: the alert was not sent: {error!r}", error=e), file=sys.stderr)
    return NeedsGrant(text)


# ---------- the owner's to-dos (C10): one key per episode ----------
KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def todo_key(project: str, name: str, episode: int) -> str:
    """A key in hubbus's KEY_RE alphabet (at most 128): credential-<project>-<name>-<n>, a long one shortened with a
    hash so it stays unique."""
    key = KEY_CHARS.sub("-", f"credential-{project}-{name}-{episode}")
    if len(key) > 128:
        h = hashlib.sha256(f"{project}/{name}".encode()).hexdigest()[:12]
        key = f"{key[:100]}-{h}-{episode}"
    return key


def owner_notify(*args: str) -> None:
    """One hubbus send as the platform (tests replace it)."""
    r = subprocess.run([*hubconf.launch(BIN / "hubbus"), "send", "--from", hubconf.hub_project(), "--to",
                        hubconf.owner_address(), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).strip()[-300:])


def raise_todo(project: str, name: str, sec: dict, why: str | None = None) -> bool:
    """This episode's owner to-do, sent once (its own record in the status, not hubbus's: a key reused after done would
    publish nothing). → whether one was sent now."""
    if sec.get("broken", "todo") != "todo":
        return False
    st = read_status(project, name)
    episode = int(st.get("episode") or 1)
    todo = st.get("todo") or {}
    if todo.get("episode") == episode and todo.get("sent"):
        return False
    doc = index()["projects"].get(project) or {}
    jobs = ", ".join(j.get("name") for j in jobs_using(doc, name)) or "-"
    text = _c("{project}: the job {jobs} needs your sign-in for {name}{why}. Run in a terminal on the hub machine: "
              "{command}", project=project, jobs=jobs, name=name, why=f" ({why})" if why else "",
              command=grant_command(project, name, sec))
    key = todo_key(project, name, episode)
    try:
        owner_notify("--type", "task", "--id", key, "--text", text, "--context", "credentials")
    except OSError as e:
        print(_("⚠️ the owner to-do {key} could not be sent ({why})", key=key, why=e), file=sys.stderr)
        return False
    st = read_status(project, name)
    st["episode"] = episode
    st["todo"] = {"episode": episode, "key": key, "sent": now_iso(), "closed": False}
    write_status(project, name, st)
    return True


def close_todo(project: str, name: str) -> None:
    st = read_status(project, name)
    todo = st.get("todo") or {}
    if not todo.get("sent") or todo.get("closed"):
        return
    try:
        owner_notify("--type", "done", "--task", todo["key"], "--text", _c("granted"), "--context", "credentials")
    except OSError as e:
        print(_("⚠️ the owner to-do {key} could not be closed ({why})", key=todo["key"], why=e), file=sys.stderr)
        return
    todo["closed"] = True
    st["todo"] = todo
    write_status(project, name, st)


def granted(project: str, name: str, sec: dict, cred: dict, route: str | None = None) -> None:
    """A new grant stored: the status is granted, this episode's to-do closed, the alert recovered."""
    _commit(cred_path(project, name, sec), cred)
    other = [b / project / f"{name}.json" for b in bases() if b != base_for(sec)]
    for o in other:
        o.unlink(missing_ok=True)
    _note(project, name, state="granted", granted_at=now_iso(), route=route or cred.get("client", {}).get("route"),
          last_result="ok", last_error=None, refresh_used_at=now_iso(), access_expires_at=cred.get("access_expires_at"))
    close_todo(project, name)
    try:
        import alert
        alert.once(f"credential:{project}:{name}", [], _("{project}/{name} works again", project=project, name=name))
    except Exception:
        pass


# ---------- the hourly job ----------
def keepalive(idx: dict | None = None, op=None) -> int:
    """brunna/credentials: for each credential a job lists, its state; an owner to-do for one that waits for the owner;
    a refresh for a refresh token unused for about a day; a file moved where its backup: setting puts it."""
    idx = idx or index()
    bad = 0
    for project, doc in sorted(idx["projects"].items()):
        for name, sec in sorted(entries(doc).items()):
            try:
                _keep(project, name, sec, doc, idx, op)
            except NeedsGrant:
                pass
            except Transient as e:
                print(_("{project}/{name}: will try again ({why})", project=project, name=name, why=e))
            except (OSError, ValueError) as e:
                bad += 1
                print(_("⚠️ {project}/{name}: {why}", project=project, name=name, why=e), file=sys.stderr)
    return 1 if bad else 0


def _keep(project, name, sec, doc, idx, op) -> None:
    used = bool(jobs_using(doc, name))
    path = found(project, name)
    st = read_status(project, name)
    if path is None:
        if st.get("state") != "not_granted":
            if st.get("state") == "granted":
                st["episode"] = int(st.get("episode") or 1) + 1
            st.update(state="not_granted", updated_at=now_iso())
            write_status(project, name, st)
        if used:
            raise_todo(project, name, sec)
        return
    if path.parent.parent != base_for(sec):   # its backup: setting changed
        with Lock(project, name, sec):
            dest = cred_path(project, name, sec)
            _write_private(dest, _read_private(path))
            path.unlink()
        path = dest
    if st.get("state") == "needs_grant":
        if used:
            raise_todo(project, name, sec)
        return
    if not st.get("state"):
        _note(project, name, state="granted")
    cred = _read_private(path)
    if cred.get("kind") == "oauth" and cred.get("refresh_token"):
        last = st.get("refresh_used_at")
        age = (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(last)).total_seconds() \
            if last else KEEPALIVE_S + 1
        if age > KEEPALIVE_S:
            refresh(project, name, idx=idx, op=op)


# ---------- status ----------
def rows(idx: dict | None = None) -> list[dict]:
    """Every declared credential's state, for check-secrets, doctor, the status page and `status`: from the status file
    and whether the file exists; never a value."""
    idx = idx or index()
    out = []
    for project, doc in sorted(idx["projects"].items()):
        for name, sec in sorted(entries(doc).items()):
            st = read_status(project, name)
            path = found(project, name)
            state = st.get("state") or ("granted" if path else "not_granted")
            if path is None:
                state = "not_granted"
            mode = path.stat().st_mode & 0o777 if path else None
            out.append({"project": project, "name": name, "kind": sec.get("kind"), "state": state,
                        "jobs": [j.get("name") for j in jobs_using(doc, name)], "server": (sec.get("server") or {}).get("url"),
                        "access_expires_at": st.get("access_expires_at"), "last_refresh": st.get("last_refresh"),
                        "last_error": st.get("last_error"), "refresh_used_at": st.get("refresh_used_at"),
                        "mode": mode, "command": grant_command(project, name, sec), "broken": sec.get("broken", "todo")})
    return out


def waiting_text(row: dict) -> str:
    """One line saying a credential waits for the owner, with the command (the status page, doctor, check-secrets)."""
    if row["state"] == "needs_grant":
        return _("{project}/{name} needs your sign-in again ({why}): {command}", project=row["project"],
                 name=row["name"], why=row.get("last_error") or "-", command=row["command"])
    return _("{project}/{name} is not granted yet; the job {jobs} doesn't run until then: {command}",
             project=row["project"], name=row["name"], jobs=", ".join(row["jobs"]) or "-", command=row["command"])


def waiting(idx: dict | None = None) -> list[dict]:
    return [r for r in rows(idx) if r["state"] != "granted" and (r["jobs"] or r["state"] == "needs_grant")]


def cmd_status(argv: list[str]) -> int:
    for r in rows():
        if argv and r["project"] not in argv:
            continue
        exp = r["access_expires_at"]
        when = datetime.datetime.fromtimestamp(exp).isoformat(timespec="minutes") if isinstance(exp, (int, float)) else "-"
        print(_("{project}/{name} ({kind}, {server}): {state}; jobs: {jobs}; access until {until}; last refresh {last}",
                project=r["project"], name=r["name"], kind=r["kind"], server=r["server"], state=r["state"],
                jobs=", ".join(r["jobs"]) or "-", until=when, last=r["last_refresh"] or "-"))
        if r["state"] != "granted":
            print("    " + waiting_text(r))
    return 0


# ---------- the shared client (route a; C2a) ----------
def shared_client() -> dict | None:
    """The shared client's pin in this release (oauth/client.json), or None."""
    try:
        d = json.loads((OAUTH / "client.json").read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def shared_client_state() -> tuple[bool, str]:
    """(route a may be used, why not). Only with oauth/published in this release and the tripwire's last check
    matching."""
    if not (OAUTH / "published").is_file() or shared_client() is None:
        return False, _("brunna's shared client isn't published in this release yet (no oauth/published)")
    st = _tripwire_status()
    if st.get("ok") is not True:
        return False, _("the published client document didn't match this install's at the last check ({why})",
                        why=st.get("why") or "-")
    return True, ""


def _tripwire_status() -> dict:
    try:
        return json.loads((hubconf.STATE / "credentials" / "shared-client.json").read_text())
    except (OSError, ValueError):
        return {}


def check_published(op=None) -> tuple[bool | None, str]:
    """The tripwire: the published document's security fields against this install's copy. (None, why) when there is
    nothing to check."""
    pin = shared_client()
    if pin is None:
        return None, _("this release ships no shared client document (oauth/client.json)")
    try:
        got = get_json(pin["client_id"], op=op)
    except Transient as e:
        return False, _("the published document could not be fetched ({why})", why=e)
    if got is None:
        return False, _("the published document could not be read as JSON")
    diff = [k for k in SECURITY_FIELDS if got.get(k) != pin.get(k)]
    return (False, _("its {fields} differ from this install's", fields=", ".join(diff))) if diff else (True, "")


def cmd_check_published() -> int:
    ok, why = check_published()
    if ok is None:
        print(why)
        return 0
    p = hubconf.STATE / "credentials" / "shared-client.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ok": ok, "why": why, "at": now_iso()}))
    if not ok:
        try:
            import alert
            alert.once("credential:shared-client", [_("brunna's published client document: {why}", why=why)],
                       _("brunna's published client document matches again"))
        except Exception:
            pass
        print(_("⚠️ brunna's published client document: {why}", why=why))
        return 1
    print(_("brunna's published client document matches this install's"))
    return 0


# ---------- provider notes ----------
def provider_notes() -> dict:
    """{issuer: note}: the shipped oauth/providers.yaml, overridden by the instance's <config>/oauth-providers.yaml."""
    import yaml
    out = {}
    for f in (OAUTH / "providers.yaml", hubconf.config_dir() / "oauth-providers.yaml"):
        try:
            d = yaml.safe_load(f.read_text()) or {}
        except (OSError, ValueError):
            continue
        for issuer, note in (d.get("providers") or {}).items():
            if isinstance(note, dict):
                out[issuer] = {**out.get(issuer, {}), **note}
    return out


def learn(issuer: str, **facts) -> None:
    """What a grant learned about an issuer, in the instance's own notes (doctor suggests sending it upstream)."""
    import yaml
    f = hubconf.config_dir() / "oauth-providers.yaml"
    try:
        d = yaml.safe_load(f.read_text()) or {}
    except (OSError, ValueError):
        d = {}
    prov = d.setdefault("providers", {})
    prov[issuer] = {**(prov.get(issuer) or {}), **facts, "checked_on": datetime.date.today().isoformat()}
    f.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=True))


# ---------- the grant (C2) ----------
def _site(host: str) -> str:
    """A host's site, roughly (its last two labels): enough to warn when the token goes elsewhere than the sign-in."""
    return ".".join((host or "").lower().split(".")[-2:])


def _challenge(header: str | None) -> dict:
    """The parameters of a WWW-Authenticate Bearer challenge."""
    out = {}
    for k, v in re.findall(r'(\w+)="([^"]*)"', header or ""):
        out[k] = v
    return out


def discover(server_url: str, op=None) -> dict:
    """C2 step 2. → {resource, issuer, as: metadata, scope, several}. ValueError when the grant must stop."""
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": names.current("cli"), "version": "0"}}}).encode()
    status, headers, _b = http("POST", server_url, data=init, op=op,
                               headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    ch = _challenge(headers.get("WWW-Authenticate") if headers else None) if status in (401, 403) else {}
    u = urllib.parse.urlsplit(server_url)
    origin = f"{u.scheme}://{u.netloc}"
    candidates = [c for c in (ch.get("resource_metadata"),
                              f"{origin}/.well-known/oauth-protected-resource{u.path.rstrip('/')}" if u.path.strip("/") else None,
                              f"{origin}/.well-known/oauth-protected-resource") if c]
    rm = None
    for c in candidates:
        if urllib.parse.urlsplit(c).scheme != "https" and (REQUIRE_HTTPS or urllib.parse.urlsplit(c).scheme != "http"):
            continue
        rm = get_json(c, op=op)
        if rm:
            break
    if not rm:
        raise ValueError(_("the server publishes no protected-resource metadata (RFC 9728); it can't be granted this way"))
    if rm.get("resource") != server_url:
        raise ValueError(_("the server's metadata names the resource {got}, not {url}: refused", got=rm.get("resource"),
                           url=server_url))
    servers = [s for s in rm.get("authorization_servers") or [] if isinstance(s, str)]
    if not servers:
        raise ValueError(_("the server names no authorization server"))
    issuer = servers[0]
    iu = urllib.parse.urlsplit(issuer)
    ipath = iu.path.rstrip("/")
    meta = None
    for c in (f"{iu.scheme}://{iu.netloc}/.well-known/oauth-authorization-server{ipath}",
              f"{issuer.rstrip('/')}/.well-known/openid-configuration"):
        meta = get_json(c, op=op)
        if meta:
            break
    if not meta:
        raise ValueError(_("the authorization server {issuer} publishes no metadata (RFC 8414)", issuer=issuer))
    if meta.get("issuer") != issuer:
        raise ValueError(_("the authorization server's metadata says its issuer is {got}, not {issuer}: refused",
                           got=meta.get("issuer"), issuer=issuer))
    if "S256" not in (meta.get("code_challenge_methods_supported") or []):
        raise ValueError(_("the authorization server doesn't say it takes PKCE S256 (code_challenge_methods_supported): "
                           "refused"))
    for k in ("authorization_endpoint", "token_endpoint"):
        scheme = urllib.parse.urlsplit(str(meta.get(k) or "")).scheme
        if scheme != "https" and (REQUIRE_HTTPS or scheme != "http"):
            raise ValueError(_("the authorization server's {key} is not https: refused", key=k))
    scope = ch.get("scope") or " ".join(rm.get("scopes_supported") or []) or None
    return {"resource": server_url, "issuer": issuer, "as": meta, "scope": scope, "several": len(servers) > 1}


def register_client(meta: dict, redirect: str, op=None) -> dict | None:
    """Route (b): RFC 7591. None when the endpoint's answer isn't a registration (unsupported, not an error)."""
    body = {"client_name": names.current("cli"), "redirect_uris": [redirect], "grant_types": ["authorization_code",
            "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}
    status, _h, raw = http("POST", meta["registration_endpoint"], data=json.dumps(body).encode(), op=op,
                           headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if status not in (200, 201) or not isinstance(d, dict) or not isinstance(d.get("client_id"), str):
        return None
    method = d.get("token_endpoint_auth_method") or "none"
    return {"route": "b", "client_id": d["client_id"], "client_secret": d.get("client_secret"),
            "auth_method": method if method in ("none", "client_secret_basic", "client_secret_post") else "none"}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def loopback(uri: str) -> bool:
    u = urllib.parse.urlsplit(uri)
    return u.scheme == "http" and u.hostname == "127.0.0.1" and not u.query and not u.fragment


def bind_callback(port: int):
    """The one-shot listener, bound to 127.0.0.1 explicitly, without address or port reuse. OSError(EADDRINUSE) when
    another process has it."""
    import http.server
    import threading
    got = {}
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(_("You can close this page and go back to the terminal.").encode())
            done.set()

    class S(http.server.HTTPServer):
        allow_reuse_address = False

    srv = S(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got, done


def port_holder(port: int) -> str:
    try:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=10)
        lines = r.stdout.strip().splitlines()[1:]
        return lines[0].split()[0] + " (pid " + lines[0].split()[1] + ")" if lines else "?"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "?"


def ask(prompt: str) -> str:
    print(prompt, end=" ", flush=True)
    return sys.stdin.readline().strip()


def refuse_unattended() -> str | None:
    if names.env("JOB") or names.env("CONFINED") or os.environ.get("HUB_LLM_POINT") or not sys.stdin.isatty():
        return _("credentials are granted by the owner, in a terminal on the hub machine, never inside a platform run")
    return None


def confirm_text(project: str, name: str, doc: dict, sec: dict, label: str, sign_in_host: str, scoped: bool) -> list[str]:
    """C2 step 4's confirmation (and set's): the jobs, their tools, the breadth, the hosts."""
    import runtime_ports
    rt = runtime_ports.load(None, ports=("credential_tools",))
    sid = sec["server"]["id"]
    token_host = urllib.parse.urlsplit(sec["server"]["url"]).hostname or ""
    lines = []
    for j in jobs_using(doc, name):
        tools = rt.rules_tools_named(j.get("allow") or [], sid)
        lines.append(_("The job {job} will use these {n} tools of {label}: {tools}.", job=j.get("name"), n=len(tools),
                       label=label, tools=", ".join(tools)))
    if not lines:
        lines.append(_("No job lists {name} yet.", name=name))
    lines.append(_("{label} publishes no narrower permissions, so the sign-in will give the platform everything your "
                   "account can do there; the platform lets only these tools through.", label=label) if not scoped else
                 _("The platform asks {label} only for the permissions listed for these tools.", label=label))
    if sign_in_host:
        lines.append(_("The sign-in is at {host}; the token will be sent to {token_host}.", host=sign_in_host,
                       token_host=token_host))
        if _site(sign_in_host) != _site(token_host):
            lines.insert(0, _("⚠️ The sign-in ({host}) and the server the token goes to ({token_host}) are on different "
                              "sites.", host=sign_in_host, token_host=token_host))
    else:
        lines.append(_("The value will be sent to {token_host}.", token_host=token_host))
    return lines


def cmd_grant(project: str, name: str, own_app: bool = False, port: int = CALLBACK_PORT) -> int:
    if (why := refuse_unattended()):
        print(why, file=sys.stderr)
        return 2
    doc, sec = entry(project, name)
    if sec.get("kind") != "oauth":
        print(_("{name} is a static credential: {command}", name=name, command=grant_command(project, name, sec)))
        return 2
    url = sec["server"]["url"]
    try:
        d = discover(url)
    except (ValueError, Transient) as e:
        print(_("✗ {why}", why=e), file=sys.stderr)
        return 1
    meta, notes = d["as"], provider_notes().get(d["issuer"], {})
    label = notes.get("label") or urllib.parse.urlsplit(d["issuer"]).hostname
    if d["several"]:
        print(_("The server lists several authorization servers; this version uses the first, {issuer}.",
                issuer=d["issuer"]))
    # the callback first: the redirect URI a registration names
    srv = None
    try:
        srv, got, done = bind_callback(port)
        port = srv.server_address[1]   # --port=0: the port the system gave
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        print(_("Port {port} is taken by {holder}; using a random port.", port=port, holder=port_holder(port)))
        if notes.get("any_port") is False:
            print(_("✗ {label} accepts only the fixed port; free it and try again", label=label), file=sys.stderr)
            return 1
        srv, got, done = bind_callback(0)
        port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    try:
        client = choose_client(meta, notes, redirect, label, own_app)
        if client is None:
            return 1
        if not loopback(client.get("redirect_uri") or redirect):
            print(_("✗ the client's redirect isn't on this machine's loopback: refused"), file=sys.stderr)
            return 1
        for line in confirm_text(project, name, doc, sec, label, urllib.parse.urlsplit(meta["authorization_endpoint"]).hostname,
                                 scoped=bool(d["scope"]) and bool(notes.get("scopes"))):
            print(line)
        if ask(_("Continue? [y/N]")).lower() not in ("y", "yes"):
            return 1
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(32)
        params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "resource": url}
        if d["scope"]:
            params["scope"] = d["scope"]
        auth_url = meta["authorization_endpoint"] + ("&" if "?" in meta["authorization_endpoint"] else "?") \
            + urllib.parse.urlencode(params)
        print(_("Opening your browser at {label}'s sign-in. If it doesn't open, or your browser is on another machine, "
                "open this link there, then paste back the address the browser ends on:", label=label))
        print(auth_url)
        try:
            import webbrowser
            webbrowser.open(auth_url)
        except Exception:
            pass
        answer = wait_callback(got, done)
        if answer is None:
            return 1
    finally:
        if srv:
            srv.shutdown()
    if answer.get("state") != state:
        print(_("✗ the answer's state doesn't match: refused"), file=sys.stderr)
        return 1
    if answer.get("error"):
        print(_("✗ {label} said: {error} {detail}", label=label, error=answer["error"],
                detail=answer.get("error_description") or ""), file=sys.stderr)
        return 1
    if meta.get("authorization_response_iss_parameter_supported") and answer.get("iss") != d["issuer"]:
        print(_("✗ the answer's issuer isn't {issuer}: refused", issuer=d["issuer"]), file=sys.stderr)
        return 1
    cred = {"v": 1, "kind": "oauth", "project": project, "name": name, "server_url": url, "issuer": d["issuer"],
            "token_endpoint": meta["token_endpoint"], "revocation_endpoint": meta.get("revocation_endpoint"),
            "resource": url, "scope": d["scope"], "client": {**client, "redirect_uri": redirect}, "granted_at": now_iso()}
    try:
        status, tokens = token_request(cred, {"grant_type": "authorization_code", "code": answer.get("code") or "",
                                              "redirect_uri": redirect, "code_verifier": verifier})
    except Transient as e:
        print(_("✗ the token endpoint couldn't be reached ({why})", why=e), file=sys.stderr)
        return 1
    if status != 200 or not tokens.get("access_token"):
        print(_("✗ the token endpoint refused the code ({error})", error=tokens.get("error") or status), file=sys.stderr)
        return 1
    cred = apply_tokens(cred, tokens)
    granted(project, name, sec, cred, client["route"])
    learn(d["issuer"], last_port="fixed" if port == CALLBACK_PORT else "random", last_route=client["route"])
    return verify(project, name, doc, sec, cred)


def choose_client(meta: dict, notes: dict, redirect: str, label: str, own_app: bool = False) -> dict | None:
    """C2 step 3: (a) when published and supported, (b) when advertised and not ruled out, else (c)."""
    if own_app:
        pass
    elif meta.get("client_id_metadata_document_supported"):
        ok, why = shared_client_state()
        if ok:
            pin = shared_client()
            return {"route": "a", "client_id": pin["client_id"], "auth_method": "none"}
        print(_("Route (a), brunna's shared client: {why}.", why=why))
    if own_app:
        pass
    elif meta.get("registration_endpoint") and notes.get("rfc7591") is not False:
        try:
            c = register_client(meta, redirect)
        except Transient as e:
            print(_("✗ the registration endpoint couldn't be reached ({why})", why=e), file=sys.stderr)
            return None
        if c:
            return c
        print(_("Route (b): {label}'s registration endpoint didn't answer as RFC 7591 registration does.", label=label))
    elif meta.get("registration_endpoint"):
        print(_("Route (b): the provider notes say {label}'s registration endpoint isn't RFC 7591.", label=label))
    print(_("Route (c): your own developer app at {label}.", label=label))
    steps = notes.get("app_steps")
    print(steps if steps else _("Create an OAuth app (client) in {label}'s developer settings, with the redirect URI "
                                "{redirect}, then enter its client ID and secret here.", label=label, redirect=redirect))
    client_id = ask(_("Client ID:"))
    if not client_id:
        return None
    import getpass
    secret = getpass.getpass(_("Client secret (empty for a public client): "))
    supported = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    method = "none" if not secret else ("client_secret_basic" if "client_secret_basic" in supported else "client_secret_post")
    return {"route": "c", "client_id": client_id, "client_secret": secret or None, "auth_method": method}


def wait_callback(got: dict, done, timeout_s: int = 300) -> dict | None:
    """The redirect's parameters: from the listener, or pasted back (the browser on another machine)."""
    import select
    deadline = time.time() + timeout_s
    print(_("Waiting for the sign-in (or paste the address here):"), flush=True)
    while time.time() < deadline:
        if done.is_set():
            return dict(got)
        r, _w, _x = select.select([sys.stdin], [], [], 1)
        if r:
            line = sys.stdin.readline().strip()
            if line:
                q = urllib.parse.urlsplit(line).query
                return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}
    choice = ask(_("No answer came. Did the sign-in page show an error? [r]etry on another port, use [y]our own app "
                   "(route c), or [q]uit:")).lower()
    if choice.startswith("r"):
        print(_("Run the command again with --port=0 (a random port)."))
    elif choice.startswith("y"):
        print(_("Run the command again with --own-app (route c)."))
    return None


def verify(project: str, name: str, doc: dict, sec: dict, cred: dict) -> int:
    """C2 step 6: initialize and tools/list through the bridge's own code, never a tools/call."""
    import credential_bridge
    import runtime_ports
    try:
        up = credential_bridge.Upstream(sec["server"]["url"], credential_bridge.header_for(sec, cred))
        tools = up.list_tools()
    except Exception as e:
        print(_("⚠️ stored, but the server didn't answer as expected: {why}", why=e))
        return 1
    rt = runtime_ports.load(None, ports=("credential_tools",))
    bad = 0
    for j in jobs_using(doc, name):
        missing = [t for t in rt.rules_tools_named(j.get("allow") or [], sec["server"]["id"]) if t not in tools]
        if missing:
            bad += 1
            print(_("⚠️ the job {job} names tools the server doesn't list: {tools}", job=j.get("name"),
                    tools=", ".join(missing)))
    print(_("✓ granted: {project}/{name} ({n} tools listed); its jobs use it from their next run", project=project,
            name=name, n=len(tools)))
    return 1 if bad else 0


def cmd_set(project: str, name: str) -> int:
    if (why := refuse_unattended()):
        print(why, file=sys.stderr)
        return 2
    doc, sec = entry(project, name)
    if sec.get("kind") != "static":
        print(_("{name} is an OAuth credential: {command}", name=name, command=grant_command(project, name, sec)))
        return 2
    for line in confirm_text(project, name, doc, sec, urllib.parse.urlsplit(sec["server"]["url"]).hostname or "", "",
                             scoped=True):
        print(line)
    if ask(_("Continue? [y/N]")).lower() not in ("y", "yes"):
        return 1
    import getpass
    value = getpass.getpass(_("Value (not shown): ")).strip()
    if not value:
        return 1
    cred = {"v": 1, "kind": "static", "project": project, "name": name, "server_url": sec["server"]["url"],
            "header": sec["server"].get("header") or "Authorization", "value": value, "granted_at": now_iso(),
            "issued_at": time.time()}
    granted(project, name, sec, cred, "static")
    return verify(project, name, doc, sec, cred)


def revoke(project: str, name: str, op=None) -> bool:
    """Revoke at the provider (RFC 7009) and delete here, even when the provider's endpoint fails (then an alert says
    to revoke at its connected-apps page). → whether the provider confirmed."""
    _doc, sec = entry(project, name)
    ok = True
    with Lock(project, name, sec):
        path = found(project, name)
        cred = None
        try:
            cred = _read_private(path) if path else None
        except (OSError, ValueError):
            pass
        if cred and cred.get("kind") == "oauth" and cred.get("revocation_endpoint") and cred.get("refresh_token"):
            form = {"token": cred["refresh_token"], "token_type_hint": "refresh_token"}
            headers = {"Content-Type": "application/x-www-form-urlencoded", **client_auth(cred["client"], form)}
            try:
                status, _h, _b = http("POST", cred["revocation_endpoint"], data=urllib.parse.urlencode(form).encode(),
                                      headers=headers, op=op)
                ok = status == 200
            except Transient:
                ok = False
        for b in bases():
            for suffix in ("", ".new"):
                (b / project / f"{name}.json{suffix}").unlink(missing_ok=True)
    st = read_status(project, name)
    st.update(state="not_granted", updated_at=now_iso(), last_result="revoked")
    write_status(project, name, st)
    if not ok:
        try:
            import alert
            alert.once(f"credential-revoke:{project}:{name}",
                       [_("{project}/{name} was deleted here, but the provider didn't confirm the revocation: revoke "
                          "it at the provider's connected-apps page", project=project, name=name)], "")
        except Exception:
            pass
    return ok


def revoke_all() -> list[str]:
    """uninstall --purge: every credential revoked before the instance goes. → what was done, one line each."""
    out = []
    for b in bases():
        if not b.is_dir():
            continue
        for f in sorted(b.glob("*/*.json")):
            project, name = f.parent.name, f.stem
            try:
                out.append(_("{project}/{name} revoked", project=project, name=name) if revoke(project, name)
                           else _("{project}/{name} deleted (revoke it at the provider)", project=project, name=name))
            except (OSError, ValueError) as e:
                out.append(f"{project}/{name}: {e}")
    return out


def rename(old: str, new: str, apply: bool) -> list[str]:
    """rename-project.sh: the credentials and their statuses follow the project. → what moves (moved when apply)."""
    out = []
    for b in [*bases(), hubconf.STATE / "credentials"]:
        src, dst = b / old, b / new
        if src.is_dir():
            out.append(f"{src} → {dst}")
            if apply:
                if dst.exists():
                    raise ValueError(_("{path} already exists", path=dst))
                os.rename(src, dst)
    return out


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_(__doc__))
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "status":
            return cmd_status(rest)
        if cmd == "grant" and len([a for a in rest if not a.startswith("--")]) == 2:
            pos = [a for a in rest if not a.startswith("--")]
            flags = [a for a in rest if a.startswith("--")]
            if set(flags) - {"--own-app", "--port=0"}:
                return 2
            return cmd_grant(*pos, own_app="--own-app" in flags, port=0 if "--port=0" in flags else CALLBACK_PORT)
        if cmd == "set" and len(rest) == 2:
            return cmd_set(*rest)
        if cmd == "revoke" and len(rest) == 2:
            ok = revoke(*rest)
            print(_("✓ revoked and deleted") if ok else _("deleted here; the provider didn't confirm: revoke it at its "
                                                          "connected-apps page"))
            return 0 if ok else 1
        if cmd == "keepalive" and not rest:
            return keepalive()
        if cmd == "check-published" and not rest:
            return cmd_check_published()
        if cmd == "refresh" and len(rest) in (2, 4):
            failed = rest[3] if len(rest) == 4 and rest[2] == "--failed" else None
            if len(rest) == 4 and failed is None:
                return 2
            scrub_env()
            try:
                refresh(rest[0], rest[1], failed=failed)
            except (NeedsGrant, Transient) as e:
                print(e, file=sys.stderr)
                return 1
            return 0
        if cmd == "rename" and len(rest) in (2, 3):
            for line in rename(rest[0], rest[1], apply="--apply" in rest[2:]):
                print(line)
            return 0
    except ValueError as e:
        print(_("✗ {why}", why=e), file=sys.stderr)
        return 1
    print(_(__doc__))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
