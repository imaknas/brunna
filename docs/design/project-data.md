# A project's data outside its folder

Status: built 11-26, after a blind review of the design and one of the code. Asked by the owner through breezyvoice (letter m11261618-0e71): "If you don't declare it, how
will the platform know in future what to back up? I mean things kept outside the project folder. Do I have to remind
every project?"

## The problem

The nightly backup (`bin/backup.py`) takes what `backup.include` in the owner's settings lists, plus each workspace's
unpushed work and `.env` files. It never takes whole workspaces. A project that keeps data it can't make again outside
its folder (breezyvoice: the owner's voice recordings, listening labels and a checker it trained, in
`~/voice-data/breezyvoice`, kept out of its public repo on purpose) is backed up only if someone notices and edits the
owner's settings by hand. On 11-26 nobody had, for weeks: the trained checker a running service uses had no copy.

The owner's backup rule (`bin/backup.py` header, 11-26): a binary that can be made again is not backed up; of models
only weights we changed ourselves are; a model that can be downloaded again never is; results that took runs to get
are data and are backed up.

## The shape

Like `secrets:`: the project declares, the platform acts and checks, the owner is not in the loop.

```yaml
data:
- path: ~/voice-data/breezyvoice
  what: the owner's recordings, listening labels, the checker we trained
  exclude: [models/, synthetic/, "fpo/runs/*/step*", "fpo/**/*.wav"]   # what can be made again
- path: ~/.cache/breezyvoice
  what: downloaded models and tool caches
  backup: false
  why: downloaded again on first use
```

- `path`: `~/…` or absolute. `what`: one line, required. `backup`: default true; `false` needs `why` (as secrets do).
  `exclude`: patterns relative to `path` (restic's syntax: `*`, `**`, a trailing `/` for a folder), never absolute,
  never `..`.
- Not a security field: a project changes it with `manifest.update`, no owner step. What makes that safe is the path
  rules below.

### Path rules (what a project may not point the backup at)

The backup runs as the owner and stores what it reads in the owner's encrypted repository, so a declared path must be
one that is plainly the project's. Two layers (blind review, 11-26):

**In the validator, the text only** (a registry entry must validate the same on every machine, and a refused file
freezes the project's whole entry, so nothing here looks at the disk): `path` is `~/…` or absolute, holds no `..`, no
glob, bracket, brace or control character (`SECRET_PATH`: `<path>/<pattern>` becomes an exclude pattern), and
- is under the home directory (not the home itself) or under `/Volumes/` (an external disk);
- is the project's own: under its workspace or runtime but not equal to either (the whole workspace stays out of the
  backup), or a path one of whose folder names is the project's name (`~/voice-data/breezyvoice`, `~/.cache/breezyvoice`);
- is not inside or around a denied place: another project's workspace or runtime, the hub checkout, the instance
  directory, the bus directory, the memory copies, git-origin, the services root, the coding agents' folders their
  adapters name, and the owner's tool, credential and personal folders (`~/.ssh`, `~/.gnupg`, `~/Library`, `~/.config`,
  `~/.aws`, `~/.docker`, `~/.kube`, `~/.netrc`, `~/.local/state`, `~/.nvm`, `~/.password-store`, `~/Documents`,
  `~/Desktop`, `~/Downloads`); a hidden folder at the top of home is a tool's, so only `~/.cache/…` and
  `~/.local/share/…` are allowed among them (`~/.claude`, `~/.codex` and the rest are refused without naming them);
- the name rule counts only folders below home (or below the disk): a project named like the account gets nothing;
- overlaps nothing in the owner's `backup.include` (restic applies every exclude to the whole run: a project's
  excludes on a folder the owner included would drop the owner's data), and isn't inside another project's entry.
  Across projects only the inner entry is refused: the outer one has its own project's name at or above it, so a
  project can't knock another's entry out by declaring a folder inside it (code review, 11-26).
- `exclude` patterns are relative, no `..`, no leading `/`.

**Where the disk is read, on the resolved path** (the backup, the daily check): the path is resolved (symlinks
followed); the resolved path must pass the same place rules. If it doesn't, that entry is skipped for the night and
reported to the project; the rest of the backup runs. The backup passes **the resolved path** to restic and builds the
excludes on it (restic matches excludes against the path as given, and stores a symlink argument as the link only).

Threat model: a sandboxed project could swap a folder above its declared path for a symlink between the check and
restic's read. What it could get that way is a copy in the owner's own encrypted backup, never out to itself; the cost
is space, and the one real harm is data meant to stay off the cloud (a work profile's) landing there. Accepted, written
down here.

### The backup

`backup.included()` adds each resolved entry with `backup` not false that exists; `cmd_run` passes each entry's
excludes as `<resolved path>/<pattern>`. `covers()` (the secrets check's view of the backup) gets one matcher written
to restic's exclude rules (`*` stays within a folder, `**` crosses folders, a trailing `/` or a folder match takes
everything below, a pattern with no `/` matches any path component), tested against restic, and used for both the
owner's and the projects' excludes (it fixes `covers()` saying a file under an excluded `node_modules` is backed up).
`backup.include` stays for what is the owner's and no project's; breezyvoice's entry moves to its declaration.

A file restic can't read makes it exit 3 with the snapshot made: the backup carries on (retention, check,
`last.json`, which records what was missed) instead of failing the whole night over one project's folder.

## The daily check

`bin/check-data.py`, a platform job (`brunna/data`, daily, `found_exit`), per declared entry:

- the resolved path passes the place rules and exists (a missing one: moved without telling the registry);
- everything in it can be read (what can't is missed by the backup: make it readable or exclude it);
- size: what the backup would take (excludes applied, symlinks not followed), recorded per day in
  `.state/project-data.json`; more than double and over 1 GB more than a week before is reported ("did something that
  can be made again land in it without an exclude?"). Each problem carries a stable id (a size changes daily), and a
  ballooned size is never kept as the baseline, so the warning stays one problem until it is dealt with.

Whether the backup ran is not this check's: the backup's own failure alert covers it once, to the right place.
Problems go through `alert.once` with key `project-data:<project>`; `alert.route` gets a case sending that prefix to
the project (owner after a day unsettled, as other routed alerts).

### Undeclared data

The same job looks for folders named exactly like a registered project (case-insensitive, names of four characters or
more) directly under `~`, `~/.cache`, `~/.local/share`, or one level down in a non-hidden folder of `~`
(`~/voice-data/breezyvoice`), over 50 MB (measured without following symlinks, stopping at 50 MB), that are not inside
any workspace, runtime, declared path, `backup.include` entry or platform root. That project gets one request letter
per path ("declare it in `data:`, with backup on or off"), recorded so a path is asked about once. Nothing is deleted
or backed up on a guess.

Shared download caches (`~/.cache/huggingface`, `~/.ollama`, `~/.cache/uv`, pip, npm) are downloads by the rule and
never asked about.

## The project rules

`templates/agent-instructions.md` gets a section next to "Credentials": when you keep anything outside the project
folder (recordings, datasets, trained weights, a model download of your own), declare it in `data:` at once; what is
not declared is not backed up and will be asked about. Downloaded models and caches are declared with `backup: false`.
The project's private data never goes into its repo.

## Left

- Tracking which project uses which shared download (a Hugging Face or Ollama model), so an unused one can be offered
  for deletion without the owner asking around. Not in this step.
- A sandboxed project writes only its own folders; a declared path outside them is not made writable by this (that
  stays `allow_write`, a security field).
