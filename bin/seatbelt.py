#!/bin/sh -c exec${IFS}"${0%/*}/py"${IFS}"$0"${IFS}"$@"
"""The macOS Seatbelt profiles the platform applies itself: neutral policy (policy.py) → SBPL text. Two kinds:
  profile(pol, cap)                      cap-runner's run jobs (+ capabilities/<name>.sb), described below
  service_profile(pol, dirs, port)       a sandboxed project's own services and command jobs (bin/sandboxed-run,
                                         through isolation.py; landlock.py is the Linux counterpart):
                                         its folders and caches writable (not the bus: its letters go through its
                                         outbox, docs/design/bus-sender.md), outbound network open, only the
                                         declared port listening

Not through any agent: a second layer can't be applied inside the runtime's sandbox (tested 10-25:
`sandbox_apply: Operation not permitted`), so this profile is applied only by cap-runner, outside the sandbox, with
`sandbox-exec -f`. Design: docs/design/capabilities.md.

The base is "allow everything, then block the dangerous parts" (a deny-list; trial period, so that Metal can run):
  write    only the project folder + the temp area (deliberately not policy.write: a run job shouldn't write the bus
           or the shared package caches directly)
  read     blocks all of workspace_root and services_root (except its own folder), other projects, policy.deny_read,
           credentials, platform state (.state whole, policy.platform_state: .logs, the data and evals dirs) and the
           bus, Keychain files; the folders above its own can be stat'ed (only
           those directories themselves: not listed, nothing in them read), or resolving the path to its own folder
           fails (git 2.56 resolves every component of `git -C <dir>` and dies on the workspace root); no raw disk
           device is read or written (/dev/disk*, /dev/rdisk*: an attached image's blocks past every read deny)
  network  fully off (127.0.0.1 and unix sockets included); Seatbelt can't filter by domain
  other    blocks the Keychain system services (the hole the runtime's sandbox can't close), opening apps, Apple Events,
           launchctl/crontab/at, inspecting or signalling other processes, and every mount (mount_denies)
Capability fragments follow the base (later rules win); the gpu fragment is empty: here a capability's role is
authorization.
Both kinds carry the git fence (git_fence_rules) for a project whose git goes through the platform, and nothing of it
for any other (docs/design/git-broker.md); for such a project the service profile also reaches, of the git broker's
sockets, only its own service socket (broker_socket_rules). The git broker's own profiles (its "Step 2 build list",
slice a), each applied by the broker from outside:
  git_writer_profile(pol, git_dirs, readable, git_paths)   P1: only the request's git dir written, minus the names git
                                                           runs or reads elsewhere; _isolation's denies and the
                                                           project's reads; no temp area, no network, the exec guard
  git_init_profile(pol, git_dir, readable, git_paths)      the broker's `git init`: only <dir>/.git written
  materialiser_profile(pol, dirs, git_paths)               P2: the service profile (the fence with it) + the exec guard

  profile(pol, cap) → str        paths are written straight into the profile (computed fresh on every run), no -D
                                 parameters
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hubconf  # noqa: E402
import names  # noqa: E402

TMP = ["/private/tmp", "/private/var/folders"]
DEVICES = ["/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/dtracehelper"]
KEYCHAIN_SERVICES = ["com.apple.SecurityServer", "com.apple.securityd.xpc", "com.apple.security.agent",
                     "com.apple.security.keychain-circle-notification", "com.apple.secd", "com.apple.trustd.agent"]
# what mounts, attaches a disk image, or changes disks on a caller's behalf (mount_denies; measured 11-15, macOS 27)
MOUNT_SERVICE_PREFIXES = ["com.apple.DiskArbitration.", "com.apple.diskimage", "com.apple.system.hdiejectd",
                          "com.apple.storagekitd", "com.apple.filesystems.", "com.apple.fskit.", "com.apple.netauth.",
                          "com.apple.automountd"]
# named on their own as well: diskutil's attach door is DiskImages2's bundled XPC service, which runs in a sandbox of
# its own and not the caller's; the prefix above catches it only because the names happen to share it
MOUNT_SERVICE_NAMES = ["com.apple.diskimagescontroller"]
# the raw devices of disks and attached images (an image the owner attached has devices the owner can read: its
# blocks, decrypted, past every read deny on where it is mounted)
DISK_DEVICES = r"^/dev/r?disk[0-9]"
MOUNT_IOKIT_SERVICES = ["IOHDIXController", "AppleDiskImagesController"]
MOUNT_IOKIT_CLIENTS = ["IOHDIXControllerUserClient"]
NO_EXEC = ["/usr/bin/open", "/usr/bin/osascript", "/bin/launchctl", "/usr/bin/crontab", "/usr/bin/at", "/usr/bin/batch",
           "/usr/bin/security"]


SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def can_sandbox() -> str | None:
    """None when this system can apply these profiles; otherwise why not (the caller refuses instead of running
    unsandboxed)."""
    import os
    if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
        return f"this system has no Seatbelt ({sys.platform})"
    return None


def guarantees() -> set[str]:
    """isolation.py's backend interface: what service_profile enforces (capabilities.md, "What each OS enforces")."""
    return {"own-writes-only", "isolated-reads", "declared-port-only", "localhost-inbound-only", "keychain-blocked",
            "signals-blocked", "runtime-files-locked", "git-dirs-fenced", "no-mounts"}


def confined_now(marked: bool = False) -> bool:
    """isolation.py's backend interface: whether this process runs under some Seatbelt profile (sandbox_check on
    itself; a runtime's session sandbox counts, the mark isn't needed). Only half the answer: any profile passes, a
    permissive one too, so isolation.confined_now also probes that the platform's state can't be read. Anything that
    can't be checked counts as not confined (the caller then wraps, or refuses)."""
    import ctypes
    import os
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        check = lib.sandbox_check
        check.restype, check.argtypes = ctypes.c_int, [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        return check(os.getpid(), None, 0) == 1
    except (OSError, AttributeError):
        return False


def exec_service(pol: dict, dirs: list, port: int | None, argv: list[str]):
    """isolation.py's adapter entry: replace this process with argv under service_profile."""
    import os
    os.execv(SANDBOX_EXEC, [SANDBOX_EXEC, "-p", service_profile(pol, dirs, port), *argv])


def q(p) -> str:
    """An SBPL string literal."""
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def glob_regex(p) -> str:
    """A glob from policy.deny_read (only * is used) → an SBPL regex, everything below included."""
    import re
    parts = [re.escape(x) for x in str(p).split("*")]
    return '#"^' + "[^/]+".join(parts).replace('"', '\\"') + '(/|$)"'


def fragment(cap: str) -> str:
    return (hubconf.ROOT / "capabilities" / f"{cap}.sb").read_text()


def spellings(p) -> list[str]:
    """A configured path and, when it differs, the path it resolves to. Seatbelt matches the real path of what is
    opened, so a rule naming only a link refuses the link while its target reads fine (a secrets folder reached through
    the instance folder <hub>/.hub-home points to); landlock.denied() resolves the same way."""
    import os
    s, r = str(p), os.path.realpath(p)
    return [s] if r == s else [s, r]


def glob_spellings(p) -> list[str]:
    """spellings() for a glob from policy.deny_read: the part before the first * resolved, the rest kept."""
    parts = pathlib.PurePath(p).parts
    i = next(n for n, x in enumerate(parts) if "*" in x)
    head, tail = pathlib.PurePath(*parts[:i]), parts[i:]
    return [str(pathlib.PurePath(h, *tail)) for h in spellings(head)]


def read_denies(pol: dict) -> list[str]:
    home = pathlib.Path.home()
    out = []
    # its own fixed list as well as the policy's platform_state: a profile built from any policy still denies .state
    for p in [hubconf.p("workspace_root"), hubconf.p("services_root"), *pol["other_projects"], *pol["credentials"],
              hubconf.STATE, hubconf.BUS_DIR, home / "Library" / "Keychains", *(pol.get("platform_state") or [])]:
        if p:
            out += [f"(subpath {q(s)})" for s in spellings(p)]
    for p in pol["deny_read"]:
        if "*" in str(p):
            out += [f"(regex {glob_regex(s)})" for s in glob_spellings(p)]
        else:
            out += [f"(subpath {q(s)})" for s in spellings(p)]
    out += [f"(literal {q(s)})" for p in pol["credential_files"] for s in spellings(p)]
    return out


def protect_denies(pol: dict) -> list[str]:
    """What its sessions load settings, hooks and tools from (policy protect): never written, after the allows (later
    rules win). A tree entry covers everything below it; the others only the folder entry itself (it can't be moved
    away, replaced or created). Anywhere, not only under the workspace: Seatbelt checks a rename at its two ends only,
    so a tree holding one of these names, built where the process may write (the temp area, its runtime folder) and
    moved into the workspace, would plant it at any depth there (docs/design/runtime-codex.md, Step 4 r2, review A-B2).
    The platform's own folders of these names are written by the platform, never from inside a project's sandbox."""
    import re
    if not pol.get("workspace") or not pol.get("protect"):
        return []
    pats = [f'#"/{re.escape(c["name"])}' + ('(/|$)"' if c["tree"] else '$"') for c in pol["protect"]]
    return ["; the files its sessions load settings and hooks from: never written, wherever they are made",
            "(deny file-write* " + " ".join(f"(regex {p})" for p in pats) + ")"]


def root_denies(pol: dict) -> list[str]:
    """The runtime's own folder in the workspace (policy protect_roots): nothing created, changed, removed or renamed
    under it but in the entries it names writable (its worktrees), nor the folder itself moved (V-M9, measured 11-09:
    docs/design/runtime-versions.md section 7). Beside protect_denies, which still covers the control names at any
    depth, those inside the writable entries included. A writable entry holds checkouts (worktrees), each with a tool
    folder of its own: that one is held the same way, one level down (not a worktree's own worktrees)."""
    import re
    ws = pol.get("workspace")
    if not ws or not pol.get("protect_roots"):
        return []
    base = pathlib.Path(ws).resolve()
    out = ["; the runtime's own folder in the project, and in each of its worktrees: nothing in it written but what it "
           "names writable"]
    for r in pol["protect_roots"]:
        top = base / r["name"]
        writable = r.get("writable") or []
        keep = " ".join(f"(require-not (subpath {q(top / w)}))" for w in writable)
        out.append(f"(deny file-write* (require-all (subpath {q(top)}){' ' + keep if keep else ''}))")
        name = re.escape(r["name"])
        for w in writable:
            inner = f"{re.escape(str(top / w))}/[^/]+/{name}"
            keep = " ".join(f'(require-not (regex #"^{inner}/{re.escape(x)}(/|$)"))' for x in writable)
            out.append(f'(deny file-write* (require-all (regex #"^{inner}(/|$)"){" " + keep if keep else ""}))')
    return out


def _glob_re(glob: str) -> str:
    """A glob of policy's (only *, within one name) → a regex for the same names."""
    import re
    return "[^/]+".join(re.escape(x) for x in glob.split("*"))


def _fence_spellings(p) -> list[str]:
    """A path of the fence: the folder above its .git resolved (both spellings, as spellings()), the rest kept as
    written. Nothing at or below the .git is resolved: the project can make entries there (a link at the platform
    folder's unused spelling, at cache/), and a profile computed from what it made would move the exception onto the
    git dir itself on the next run."""
    parts = pathlib.PurePosixPath(p).parts
    i = parts.index(".git") if ".git" in parts else len(parts)
    return [str(pathlib.PurePosixPath(h, *parts[i:])) for h in spellings(pathlib.PurePosixPath(*parts[:i]))]


def git_fence_rules(pol: dict) -> list[str]:
    """The git fence (policy git_fence; docs/design/git-broker.md, "The fence", r2 B2), for a project whose git goes
    through the platform; nothing at all for any other. Denies only, after the allows, so nothing written later opens
    it again (the repo's platform folder is kept out of the deny, never allowed back: an allow there would reopen the
    control-file denies under it too):
    - no entry named .git, nor anything below one, anywhere: not in its folders, not in the temp area (a tree built
      there and moved in is checked only at the rename's two ends), with each repo's platform folder left out;
    - no entry named HEAD that isn't a folder, anywhere (no git-dir shape under another name; a folder HEAD/ is
      ordinary), and no folder HEAD/ removed or renamed: a swap (renamex_np RENAME_SWAP) is checked with each end's
      existing type, so a folder HEAD/ swapped with a file would land a HEAD file anywhere (step 1's second cold
      review; the cost: a fenced writer can't remove or rename a folder named HEAD);
    - in each repo's platform folder: no .git below it but uv's marker, exactly, and only as a regular file (a folder
      moved onto the marker's name would be a git dir); the folder itself is made by the platform, never from inside
      (nor removed or renamed), and every folder on the way to the marker can't be removed or renamed (moving the
      bucket out would carry the marker with it), nor made as a link.
    Its paths are written as the policy names them below the folder that holds the .git (_fence_spellings): a link
    the project made there leads a write elsewhere, where the deny matches it, and never moves the exception.
    Seatbelt matches the path the volume resolves, so a folded spelling (.GIT, head) is the same entry (measured,
    git-config-fence.md), and a write through a link is matched where it lands."""
    import re
    f = pol.get("git_fence")
    if not f:
        return []
    names = "|".join(re.escape(n) for n in f["names"])
    files = "|".join(re.escape(n) for n in f["files"])
    dirs = [s for d in f["repo_dirs"] for s in _fence_spellings(d)]
    keep = "".join(f" (require-not (subpath {q(d)}))" for d in dirs)
    out = ["; git dirs (git through the platform): no .git anywhere, no HEAD that isn't a folder, each repo's platform "
           "folder kept",
           f'(deny file-write* (require-all (regex #"/({names})(/|$)"){keep}))',
           f'(deny file-write* (require-all (regex #"/({files})$") (require-not (vnode-type DIRECTORY))))',
           f'(deny file-write-unlink (require-all (regex #"/({files})$") (vnode-type DIRECTORY)))']
    for d in dirs:
        marks = []
        for m in f.get("markers") or []:
            for root in _fence_spellings(m["root"]):
                if not (root == d or root.startswith(d + "/")):
                    continue
                marker = f'(regex #"^{re.escape(root)}/{_glob_re(m["glob"])}$")'
                marks.append(f"(require-not {marker})")
                out.append(f"(deny file-write* (require-all {marker} (require-not (vnode-type REGULAR-FILE))))")
                # the folders from d down to the marker's own folder: pinned as entries
                parts = pathlib.PurePosixPath(root).relative_to(d).parts + pathlib.PurePosixPath(m["glob"]).parts[:-1]
                for i in range(1, len(parts) + 1):
                    path = "/".join([re.escape(d), *(_glob_re(p) for p in parts[:i])])
                    out += [f'(deny file-write-unlink (require-all (regex #"^{path}$") (vnode-type DIRECTORY)))',
                            f'(deny file-write-create (require-all (regex #"^{path}$") (vnode-type SYMLINK)))']
        out.append(f'(deny file-write* (require-all (regex #"^{re.escape(d)}/(.*/)?({names})(/|$)")'
                   + "".join(" " + x for x in marks) + "))")
        out += [f"(deny file-write-unlink (literal {q(d)}))", f"(deny file-write-create (literal {q(d)}))"]
    return out


def mount_denies() -> list[str]:
    """No mount from inside, anywhere (guarantee no-mounts; docs/design/capabilities.md, "11-15: no mounts"): a disk
    image the process wrote, attached and mounted over a folder, puts files it never wrote through the file system
    (a .git with a hostile config) where its write rules don't reach. Four layers, each measured on its own route:
    - the mount operations themselves: mount(2), a remount and unmount by the process (mount_devfs, mount_hfs,
      mount_nfs, mount_smbfs, …), and a mount a daemon makes at a mount point the caller names (DiskArbitration
      checks the caller's sandbox: `hdiutil attach -mountpoint`, `diskutil mount -mountPoint`);
    - the services that attach images, mount at a place they choose (/Volumes), unmount or change disks for a caller:
      DiskArbitration, diskimagescontroller (DiskImages2's bundled XPC service: diskutil image attach goes through
      it, and it reaches diskimagesiod from its own sandbox, so denying diskimagesiod alone leaves the attach
      working), diskimagesiod/spawner, hdiejectd, storagekitd, FSKit/UserFS, NetAuth, the automounter; by name, as
      Seatbelt can only deny a mach or XPC service it is told about (a bundled XPC service is matched by global-name
      too, measured);
    - the disk-image driver's own door (hdiutil opens IOHDIXController's user client directly to make the device);
    - LaunchServices, `(deny lsopen)` in _isolation (not repeated here): opening a .dmg starts DiskImageMounter outside
      the sandbox, which attaches and mounts it at /Volumes; with this block alone and lsopen allowed, it does.
    Not an exec deny: that names one path, and the door is the services (a copied hdiutil, killed at exec on macOS 27
    by code signing, is not a promise)."""
    return [
        "; no mounts: none made, remounted or unmounted by the process, none asked of the services that mount, attach "
        "disk images or change disks, nor of the disk-image driver (LaunchServices: the lsopen deny below)",
        "(deny file-mount)",
        "(deny file-mount-update)",
        "(deny file-unmount)",
        "(deny mach-lookup " + " ".join([*(f"(global-name-prefix {q(s)})" for s in MOUNT_SERVICE_PREFIXES),
                                         *(f"(global-name {q(s)})" for s in MOUNT_SERVICE_NAMES)]) + ")",
        "(deny iokit-open-service " + " ".join(f"(iokit-registry-entry-class {q(c)})" for c in MOUNT_IOKIT_SERVICES)
        + ")",
        "(deny iokit-open-user-client " + " ".join(f"(iokit-user-client-class {q(c)})" for c in MOUNT_IOKIT_CLIENTS)
        + ")",
    ]


def ancestors(paths) -> list[str]:
    """The folders above each path, as literals: what a process must be able to stat to resolve the path to a folder
    it may use (realpath, `git -C`), when one of them is in a denied tree (the workspace root, the platform state).
    Both the folders above the path as given and above what it resolves to: resolving walks the first, then the
    second (a denied tree may be named by either spelling, read_denies)."""
    import os
    above = {a for p in paths for s in {os.path.abspath(p), os.path.realpath(p)} for a in pathlib.PurePath(s).parents}
    return [f"(literal {q(a)})" for a in sorted(above, key=str)]


def _isolation(pol: dict, writable: list[pathlib.Path], writable_extra: list[pathlib.Path] = (),
               readable: list[pathlib.Path] = (), temp: bool = True) -> list[str]:
    """What both kinds share: writes only to `writable` (+ `writable_extra`) and the temp area, reads blocked for other
    projects, platform state and credentials (own folders allowed again, `readable` too, the folders above them stat'ed
    only), Keychain services, other processes, launching outside. The paths come as configured: the allows name what they
    resolve to (Seatbelt matches the real path, so an allow naming a link never matches), the stat-only folders are
    above both spellings (ancestors). temp=False: the temp area leaves the write allow (the git broker's writer, P1),
    the devices stay; the output for temp=True is unchanged."""
    configured = [*writable, *writable_extra, *readable]
    own = [f"(subpath {q(pathlib.Path(w).resolve())})" for w in [*writable, *writable_extra]]
    readable = [pathlib.Path(r).resolve() for r in readable]
    return [
        "; writes: only its own folders and the temp area (the allow after this wins)" if temp else
        "; writes: only its own folders (the allow after this wins)",
        '(deny file-write* (subpath "/"))',
        "(allow file-write* " + " ".join([*own, *[f"(subpath {q(t)})" for t in (TMP if temp else [])],
                                          *[f"(literal {q(d)})" for d in DEVICES]]) + ")",
        *protect_denies(pol),
        *root_denies(pol),
        *git_fence_rules(pol),
        "; reads: other projects, services, platform state, credentials, Keychain files; its own folders allowed again",
        "(deny file-read* " + " ".join(read_denies(pol)) + ")",
        "(allow file-read* " + " ".join(own + [f"(subpath {q(r)})" for r in readable]) + ")",
        "; the folders above them: stat only (resolving a path to its own folder walks through them)",
        "(allow file-read-metadata " + " ".join(ancestors(configured)) + ")",
        "; the raw devices of disks and attached images: their blocks would bypass every read deny above",
        f'(deny file-read* file-write* (regex #"{DISK_DEVICES}"))',
        "; the Keychain system services",
        "(deny mach-lookup " + " ".join(f"(global-name {q(s)})" for s in KEYCHAIN_SERVICES) + ")",
        *mount_denies(),
        "; no asking other system services to launch programs outside the sandbox, no touching other processes",
        "(deny lsopen)",
        "(deny appleevent-send)",
        "(deny process-exec " + " ".join(f"(literal {q(s)})" for x in NO_EXEC for s in spellings(x)) + ")",
        # tested 10-25: (deny signal (target others)) doesn't block it; block everything first, then allow the same sandbox
        "(deny process-info*)",
        "(allow process-info* (target same-sandbox))",
        "(deny signal)",
        "(allow signal (target same-sandbox))",
    ]


def profile(pol: dict, cap: str) -> str:
    ws = pathlib.Path(pol["workspace"]).resolve()
    lines = [
        "(version 1)",
        f"; agent-hub platform-run: capability {cap} (generated by bin/seatbelt.py on every run; don't edit)",
        "(allow default)",
        "; network: none at all (including localhost and unix sockets)",
        "(deny network-outbound (remote ip))",
        "(deny network-outbound (remote unix-socket))",
        "(deny network-inbound (local ip))",
        "(deny network-bind)",
        *_isolation(pol, [ws]),
        f"; ---------- capability {cap} (capabilities/{cap}.sb) ----------",
        fragment(cap).rstrip(),
        "",
    ]
    return "\n".join(lines)


def service_profile(pol: dict, dirs: list[pathlib.Path], port: int | None = None) -> str:
    """A sandboxed project's own service or command job (bin/sandboxed-run): like a session of that project, not like
    cap-run. dirs = its runtime and workspace (read and write); policy.write may be written too. The bus directory
    is neither readable nor writable: agenthub send writes into the project's outbox (docs/design/bus-sender.md). Network: outbound is open: Seatbelt can't filter
    by domain, so the project's network list is not enforced at this layer (what this layer guarantees is the files,
    Keychain and processes); listening only on the port the service declares in expose.local_port, and only
    connections through localhost are accepted (not from the LAN or the tailnet directly: tailscale serve)."""
    lines = [
        "(version 1)",
        "; agent-hub: a sandboxed project's own service or job (generated by bin/seatbelt.py on every run; don't edit)",
        "(allow default)",
        "; network: outbound open (Seatbelt can't filter by domain); listening only on the declared port",
        "(deny network-bind)",
        "(deny network-inbound (local ip))",
    ]
    if port:
        # tested 10-31: bind is matched by port (0.0.0.0 gets through too); inbound per port is unreliable (listen()
        # refused on some ports, allowed on others), so inbound is localhost on any port: only the declared port can be
        # bound at all, and connections arriving on the LAN or tailnet address are not accepted (tailscale serve
        # connects through localhost)
        lines += [f'(allow network-bind (local ip "*:{int(port)}"))',
                  '(allow network-inbound (local ip "localhost:*"))']
    lines += broker_socket_rules(pol)
    # policy.write is written too; the registry index the platform CLI reads (agenthub send / complete inside a
    # service): read-only
    lines += _isolation(pol, [pathlib.Path(d) for d in dirs], [hubconf.path(w) for w in pol["write"]],
                        [pathlib.Path(r) for r in pol.get("read") or []])
    return "\n".join(lines + [""])


# ---------- the git broker's profiles (docs/design/git-broker.md, "Step 2 build list", slice a) ----------
# P1, the git-dir writer: the names in a git dir that git runs or reads as instructions elsewhere, or that change what
# the owner's git sees, never written (each a regex below the git dir, matched in any letter case: the volume folds);
# the entries git needs stay pinned (not removed, renamed or made as a link). r2 B1, r3 S-b, open point 22.
GIT_WRITER_DENIED = ("config", "commondir", "gitdir", "shallow", "hooks(/|$)", "modules(/|$)", "remotes(/|$)",
                     "branches(/|$)", "info/attributes", "info/grafts", "objects/info/alternates",
                     "objects/info/http-alternates", "worktrees/[^/]+/(commondir|gitdir|config)")
GIT_WRITER_PINS = ("objects", "objects/info", "refs", "logs", "worktrees", "info")


def _anycase(text: str) -> str:
    """A regex fragment matching text in any letter case (Seatbelt's regex has no flag for it)."""
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in text)


def broker_socket_rules(pol: dict) -> list[str]:
    """For a project whose git goes through the platform (policy git_broker): no Unix socket in the broker's folder but
    its own service socket (r2 S5, measured: an unrelated socket stays reachable; r1 S5: the whole broker folder, so
    its own session socket too). Nothing at all for any other project."""
    gb = pol.get("git_broker")
    if not gb:
        return []
    return ["; the platform's git broker: of the sockets in its folder only this project's own service socket",
            "(deny network-outbound (remote unix-socket " + " ".join(f"(subpath {q(s)})" for s in spellings(gb["root"]))
            + "))",
            "(allow network-outbound (remote unix-socket "
            + " ".join(f"(path-literal {q(s)})" for s in spellings(gb["service"])) + "))"]


def exec_guard(git_paths) -> list[str]:
    """No program runs but git's own (r2 B1, measured in section 4): `git_paths` are its binaries, and its exec-path
    folder written with a trailing /; resolved by the caller (the broker, when it starts). Last in a profile: its allow
    names only git, so it reopens nothing else."""
    allows = []
    for p in git_paths:
        s = str(p)
        allows += [f"(subpath {q(x)})" for x in spellings(s.rstrip("/"))] if s.endswith("/") else \
                  [f"(literal {q(x)})" for x in spellings(s)]
    if not allows:
        raise ValueError("the exec guard needs git's own programs")
    return ["; the exec guard: no program runs but git's own", "(deny process-exec)",
            "(allow process-exec " + " ".join(dict.fromkeys(allows)) + ")"]


def _no_network() -> list[str]:
    return ["; network: none at all (including localhost and unix sockets)",
            "(deny network-outbound (remote ip))",
            "(deny network-outbound (remote unix-socket))",
            "(deny network-inbound (local ip))",
            "(deny network-bind)"]


def _read_rules_present(pol: dict) -> None:
    """A git-dir writer is never built from a policy without the project's read rules (r3 S-b: such a profile put another
    project's file into the objects)."""
    missing = [k for k in ("workspace", "other_projects", "deny_read", "credentials", "credential_files") if k not in pol]
    if missing or not read_denies(pol):
        raise ValueError(f"no git-dir writer without the project's read rules (missing: {', '.join(missing) or 'all'})")


def git_dir_rules(git_dirs) -> list[str]:
    """P1's denied names and pins below each git dir (as resolved: Seatbelt matches the real path)."""
    import re
    out = ["; the git dir: no name git runs or reads as instructions; its own entries pinned"]
    for g in git_dirs:
        for d in dict.fromkeys(spellings(g)):
            base = re.escape(d)
            out.append("(deny file-write* " + " ".join(f'(regex #"^{base}/{_anycase(n)}")' for n in GIT_WRITER_DENIED)
                       + ")")
            pins = "|".join(_anycase(re.escape(n)) for n in GIT_WRITER_PINS)
            own = "|".join(_anycase(re.escape(n)) for n in names.all("repo_dir"))
            out += [f'(deny file-write-unlink (regex #"^{base}/({pins}|{own})$"))',
                    f'(deny file-write-create (require-all (regex #"^{base}/({pins}|{own})$") (vnode-type SYMLINK)))']
    return out


def git_writer_profile(pol: dict, git_dirs, readable, git_paths) -> str:
    """P1 (r2 B1, r3 S-b): writes only the request's git dir (its common dir; a linked worktree's admin dir is inside
    it), minus the names above, and nothing else: no worktree, no temp area, only the devices. Every deny of
    _isolation (mounts, raw disks, lsopen, the Keychain, other processes) and the project's own read rules, with
    `readable` its own folders (its workspace, runtime folder, policy read, and at most one bundle for one call); no
    network at all; the exec guard last. Never the fence: it must write a git dir."""
    _read_rules_present(pol)
    return "\n".join([
        "(version 1)",
        "; brunna: the git broker's git-dir writer (generated by bin/seatbelt.py for each call; don't edit)",
        "(allow default)",
        *_no_network(),
        *_isolation({**pol, "git_fence": None}, [pathlib.Path(g) for g in git_dirs], (),
                    [pathlib.Path(r) for r in readable], temp=False),
        *git_dir_rules(git_dirs),
        *exec_guard(git_paths),
        "",
    ])


def git_init_profile(pol: dict, git_dir, readable, git_paths) -> str:
    """The broker's `git init` (r1 S1, r3 S-a): P1's rules with the new repo's <real path>/.git the only writable
    folder, and without P1's denied names and pins, which init must write (config, objects, refs, info); the config op
    replaces the config whole afterwards."""
    _read_rules_present(pol)
    return "\n".join([
        "(version 1)",
        "; brunna: the git broker's init (generated by bin/seatbelt.py for each call; don't edit)",
        "(allow default)",
        *_no_network(),
        *_isolation({**pol, "git_fence": None}, [pathlib.Path(git_dir)], (), [pathlib.Path(r) for r in readable],
                    temp=False),
        *exec_guard(git_paths),
        "",
    ])


def materialiser_profile(pol: dict, dirs, git_paths) -> str:
    """P2 (r2 B1; open point 3): the project's own service profile unchanged, the fence with it, plus the exec guard, so
    it runs only git and copies nothing itself."""
    return service_profile(pol, dirs) + "\n".join(exec_guard(git_paths)) + "\n"


if __name__ == "__main__":  # seatbelt.py <project> <capability>: print the profile it would use (for debugging)
    import policy
    print(profile(policy.for_project(sys.argv[1]), sys.argv[2]))
