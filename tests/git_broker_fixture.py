"""Shared by the git broker's tests (docs/design/git-broker.md, "Step 2 build list", slice a): a fixed, fake policy whose
renderings don't depend on this machine (the golden of step 1's profiles), and the throwaway projects the real-Seatbelt
tests run against. Nothing here reads or writes a live project, the live state or the owner's git config.
"""
import contextlib
import os
import pathlib
import sys
from unittest import mock

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import hubconf  # noqa: E402

FAKE = pathlib.PurePosixPath("/fake")


def fake_policy(**kw) -> dict:
    """A policy of every kind of entry the renderers read, at paths that exist nowhere."""
    ws = FAKE / "workspace" / "proj"
    return {
        "workspace": pathlib.Path(ws),
        "write": ["/fake/services/proj", "/fake/allow"],
        "env": {},
        "protect": [{"name": ".tool", "tree": True}, {"name": "TOOL.md", "tree": False}],
        "protect_roots": [{"name": ".tool", "writable": ["worktrees"]}],
        "other_projects": [pathlib.Path("/fake/workspace/other"), pathlib.Path("/fake/git-origin/other-memory.git")],
        "deny_write": [], "deny_write_files": [],
        "deny_read": [pathlib.Path("/fake/memory-copies/*/memory"), pathlib.Path("/fake/secrets")],
        "credentials": [pathlib.Path("/fake/home/.ssh")],
        "credential_files": [pathlib.Path("/fake/home/.netrc")],
        "platform_state": [pathlib.Path("/fake/hub/.logs")],
        "read": [pathlib.Path("/fake/hub/.shared")],
        "network": [], "local_bind": True, "platform_commands": [], "platform_cli": [], "capabilities": [],
        "git_fence": None,
        **kw,
    }


@contextlib.contextmanager
def fixed_machine():
    """hubconf's places and the home folder at fake paths, and no path resolved through this machine's links (macOS
    /tmp, Linux's merged /bin): a rendering then reads the same on every OS and machine."""
    import seatbelt
    with mock.patch.object(hubconf, "p", lambda key, default=None: {"workspace_root": FAKE / "workspace",
                                                                   "services_root": FAKE / "services"}.get(key)), \
            mock.patch.object(hubconf, "STATE", pathlib.Path("/fake/hub/.state")), \
            mock.patch.object(hubconf, "BUS_DIR", pathlib.Path("/fake/hub/.state/bus")), \
            mock.patch.object(pathlib.Path, "home", classmethod(lambda cls: cls("/fake/home"))), \
            mock.patch.object(os.path, "realpath", lambda p, **k: os.path.abspath(p)), \
            mock.patch.object(seatbelt, "TMP", ["/fake/tmp", "/fake/var/folders"]):
        yield


def off_renderings() -> dict[str, str]:
    """_isolation's and service_profile's output for the fake policy, switched off (step 1's golden)."""
    import seatbelt
    with fixed_machine():
        pol = fake_policy()
        dirs = [pathlib.Path("/fake/services/proj"), pathlib.Path("/fake/workspace/proj")]
        return {
            "_isolation": "\n".join(seatbelt._isolation(pol, dirs, [pathlib.Path("/fake/allow")],
                                                        [pathlib.Path("/fake/hub/.shared")])),
            "service_profile": seatbelt.service_profile(pol, dirs),
            "service_profile_port": seatbelt.service_profile(pol, dirs, 18000),
        }
