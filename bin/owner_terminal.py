"""Whether this process looks like the owner at their own terminal, for the commands only the owner runs and whose yes
must be theirs (`brunna skills sync`, `brunna release`). One check, each command words its own refusal.

What it refuses is where it can tell it runs inside a sandbox or a coding agent's session: a guard against routine or
accidental use, not a boundary. An unsandboxed agent runs as the owner and holds the owner's logins anyway (and the
session check reads its environment, which such an agent can change). For a sandboxed project the boundary is its
sandbox, which denies the owner's credentials to its sessions.

  refusal() -> None | "sandbox" | "session" | "worktree"
      sandbox   inside a project's sandbox: isolation.confined_now, or simply the platform's state unreadable, which is
                what any project sandbox does (on Linux too without sandboxed-run's mark)
      session   inside a coding agent's session (runtime_ports.current_session): an agent must not be able to say yes
      worktree  a linked worktree of the hub: the state such a command writes is the live one
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402


def refusal() -> str | None:
    import isolation
    import runtime_ports
    if isolation.confined_now() or isolation.reads_blocked():
        return "sandbox"
    if runtime_ports.current_session():
        return "session"
    if hubconf.worktree_lookup():
        return "worktree"
    return None
