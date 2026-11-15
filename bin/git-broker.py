#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The git broker service (docs/design/git-broker.md; the library and its protocol: bin/git_broker.py). In step 2 only
tests start it: no service is declared until a throwaway project is first switched on (step 6, open point 12: the
sandbox module, beside cap-runner).

  git-broker.py        bind every switched-on project's two sockets and serve until stopped (SIGTERM, SIGINT)
On a system whose sandbox can't fence git dirs (Linux's Landlock) it says so and exits 2, having run nothing.
"""
from __future__ import annotations

import pathlib
import signal
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import git_broker  # noqa: E402


def main(argv: list[str]) -> int:
    if argv:
        print(__doc__)
        return 0 if argv[0] in ("-h", "--help") else 2
    broker = git_broker.Broker()
    try:
        broker.start()
    except git_broker.Answer as a:
        print(a, file=sys.stderr)
        return a.code
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_a: stop.set())
    try:
        broker.serve(stop)
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
