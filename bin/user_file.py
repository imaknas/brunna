"""Replacing a file that is there already, whole (a file beside it, synced, renamed over it), as its owner has it: the mode
stays, and a link stays a link (the file it points to is replaced). For the user's own files and the settings people edit;
hubconf.write_atomic is for files the platform makes."""
from __future__ import annotations

import os
import pathlib
import secrets
import stat


def replace(path, text: str | bytes, mode: int | None = None) -> None:
    """mode: for a file that isn't there yet (else 0644 less the umask, as any new file's). Text is written as it is, its line
    ends too."""
    path = pathlib.Path(path)
    target = pathlib.Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        if mode is None:
            mask = os.umask(0)
            os.umask(mask)
            mode = 0o666 & ~mask
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)   # before the rename: never readable by more than the old one was
            fh.write(text.encode() if isinstance(text, str) else text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
