#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The platform's `git` (docs/design/git-broker.md, "The wrapper"; "Step 2 build list", slice a): first on PATH for a
sandboxed process of a project whose git goes through the platform (bin/git-wrapper/git, put there by sandboxed-run for
its services and jobs), and `brunna git <args>` for any agent or tool whose PATH the platform can't set.

  - Outside a sandbox (isolation.confined_now() false: the owner's tools, an agent's own git) it execs the real git with
    argv, environment and stdio unchanged.
  - Inside one, by git_allow.classify:
      in place   the real git, here, as the caller (the read forms; a call whose environment selects a repo, index or
                 object folder runs unchanged, and a write to a git dir then fails loudly with the fence's refusal)
      refused    exit 2, "can't be done here (why); nothing was run"
      forwarded  to the broker on the socket sandboxed-run names (<prefix>GIT_SOCKET): the nonce file created in the
                 project's folder as proof, the request, stdin (unless it is a terminal), and back stdout, stderr and
                 the exit code; when the broker can't be reached the call says so and fails (exit 2): writes need it,
                 reads run in place anyway. A process given no socket has no broker: its git runs in place.
The real git is the first `git` on PATH that isn't this folder's (so the wrapper never finds itself).
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import i18n  # noqa: E402
import names  # noqa: E402

_ = i18n.domain(__file__)
WRAPPER_DIR = pathlib.Path(__file__).resolve().parent / "git-wrapper"


def real_git() -> str:
    own = os.path.realpath(WRAPPER_DIR)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or os.path.realpath(d) == own:
            continue
        p = os.path.join(d, "git")
        if os.path.isfile(p) and os.access(p, os.X_OK) and os.path.realpath(os.path.dirname(os.path.realpath(p))) != own:
            return p
    return "/usr/bin/git"


def exec_real(argv: list[str]):
    git = real_git()
    os.execv(git, [git, *argv])


def inside() -> bool:
    import isolation
    return isolation.confined_now()


def say(text: str) -> None:
    sys.stderr.write(text.rstrip("\n") + "\n")
    sys.stderr.flush()


def main(argv: list[str]) -> int:
    if not inside():
        exec_real(argv)
    import git_allow
    env = git_allow.relevant_env(os.environ)
    call = git_allow.classify(argv, env)
    if call.kind == git_allow.IN_PLACE:
        exec_real(argv)
    if call.kind == git_allow.REFUSED:
        say(git_allow.message(call))
        return 2
    path = names.env("GIT_SOCKET")
    if not path:
        exec_real(argv)
    import git_broker
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        git_broker.unix_connect(s, path)
    except OSError as e:
        say(_("git: the platform's git broker can't be reached ({why}): git {sub} writes through it, so it can't be "
              "done now; nothing was run", why=e.strerror or e, sub=call.sub))
        return 2
    try:
        return converse(s, argv, env, git_broker)
    finally:
        s.close()


def converse(s: socket.socket, argv: list[str], env: dict, gb) -> int:
    nonce_file = None
    try:
        while True:
            got = gb.recv(s)
            if got is None:
                say(_("git: the platform's git broker closed the connection; the call's outcome is unknown"))
                return 2
            kind, data = got
            if kind == b"N":
                msg = json.loads(data)
                d = pathlib.Path(msg["dir"])
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    fd = os.open(d / msg["nonce"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    os.close(fd)
                    nonce_file = d / msg["nonce"]
                except OSError as e:
                    say(_("git: this process can't prove the project the broker serves on this socket ({why}); nothing "
                          "was run", why=e.strerror or e))
                    return 2
                gb.send(s, b"P")
                gb.send(s, b"R", json.dumps({"argv": argv, "cwd": os.getcwd(), "env": env}).encode())
                threading.Thread(target=pump_stdin, args=(s, gb), daemon=True).start()
            elif kind == b"O":
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            elif kind == b"E":
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
            elif kind == b"X":
                return int(json.loads(data).get("code", 2))
    finally:
        if nonce_file is not None:
            try:
                os.unlink(nonce_file)
            except OSError:
                pass


def pump_stdin(s: socket.socket, gb) -> None:
    try:
        if not sys.stdin or sys.stdin.isatty():
            gb.send(s, b"I")
            return
        while chunk := os.read(sys.stdin.fileno(), 1 << 16):
            gb.send(s, b"I", chunk)
        gb.send(s, b"I")
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
