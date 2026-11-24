"""How a long-running platform service follows the platform's code (docs/design/service-code.md D3–D5).

A service keeps the modules it loaded when it started. It notes the checkout's commit then (hubconf.code_rev), checks
whether the checkout has moved on, and exits at the first moment it has nothing in flight; the service manager
(launchd KeepAlive, systemd Restart=always) starts it again on the new code. What counts as in flight is the service's
to say; this module only answers "has the code moved on".

  Watch(name)        notes the code at start (hubconf.LOADED_REV: the process's own, not a later read) and writes the
                     service's record, <state>/services/<name>.json = {pid, rev, started}: render-services reads it to
                     say when a service stays on old code (D5)
  Watch.stale()      the checkout names another commit and `brunna land` has marked it landed (hubconf.landed_rev:
                     after merge-branch's fast-forward, and in upgrade after setup-python and the config steps, so a
                     service never starts on new code before what it needs is in place), or the checkout has named it
                     for LAND_GRACE_S with no land (a manual move; a land that failed). Where ROOT isn't a git checkout
                     (rev None; a reftable repo): bin/'s newest modification time moved and has been still for SETTLE_S.
                     An error is "not stale this time" (reported to on_error), never an exception: a check that raised
                     ended lease-runner's idle thread for good
  Watch.draining(why) while stale and kept by work in flight: when it first was, and what keeps it, in the record (the
                     alert names it)
  preload(names)     imports each platform module the service would otherwise import lazily, so a service draining on old
                     code is one consistent snapshot of it (D4; tests/test_service_code.py checks each service's list)
  record(name)       a service's record, or None

Standard library + hubconf.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402

SETTLE_S = 60
LAND_GRACE_S = 600


def record_path(name: str) -> pathlib.Path:
    return hubconf.STATE / "services" / f"{name}.json"


def record(name: str) -> dict | None:
    try:
        d = json.loads(record_path(name).read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


class Watch:
    def __init__(self, name: str, on_error=None):
        self.name = name
        self.on_error = on_error
        self.rev = hubconf.LOADED_REV
        self.stamp = hubconf.code_stamp() if self.rev is None else 0.0
        self.started = time.time()
        self.moved: tuple[str, float] | None = None   # (the commit the checkout moved to, when this first saw it)
        self.rec = {"pid": os.getpid(), "rev": self.rev, "started": round(self.started, 3)}
        self._write()

    def _error(self, e: BaseException) -> None:
        if self.on_error:
            try:
                self.on_error(e)
            except Exception:
                pass

    def _write(self) -> None:
        try:
            hubconf.write_atomic(record_path(self.name), json.dumps(self.rec))
        except OSError as e:   # the record only feeds a check; the service runs without it
            self._error(e)

    def stale(self) -> bool:
        try:
            if self.rev is None:
                stamp = hubconf.code_stamp()
                return stamp > self.stamp and time.time() - stamp >= SETTLE_S
            now = hubconf.code_rev()
            if now is None or now == self.rev:
                self.moved = None
                return False
            if not self.moved or self.moved[0] != now:
                self.moved = (now, time.time())
            return hubconf.landed_rev() == now or time.time() - self.moved[1] >= LAND_GRACE_S
        except Exception as e:
            self._error(e)
            return False

    def draining(self, why: str | None) -> None:
        """What keeps a stale service from exiting (None: nothing yet), kept in the record when it changes."""
        rec = dict(self.rec)
        if why:
            rec.setdefault("stale_since", round(time.time(), 3))
            rec["draining"] = why
        else:
            rec.pop("draining", None)
        if rec != self.rec:
            self.rec = rec
            self._write()


def preload(names) -> None:
    for n in names:
        importlib.import_module(n)
