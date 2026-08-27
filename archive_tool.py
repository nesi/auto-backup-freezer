#!/bin/python
"""
archive_tool - manage scrontab entries that run runner_archive.

Tool 2 (SPEC.md "How it works"). Subcommands, not flags, because the
operations have genuinely different parameters (status needs no
folder/bucket; remove --all needs neither folder nor pattern) - see
design discussion. Skeleton only - see TODOs.

Multiple entries can coexist: an entry's identity is derived from its
folder+pattern (entry_id()), embedded in its scrontab marker, so two
different folder/pattern combinations never collide, and re-running
`add` for the same folder/pattern updates that one entry in place.
"""

import getopt
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROGNAME = "archive_tool"
MARKER_PREFIX = "# managed-by-archive_tool"
DEFAULT_SCHEDULE = "0 2 * * *"  # arbitrary (daily, 2am) - SPEC only says "default: daily", exact time not discussed
# Must match runner_archive.py's DEFAULT_PATTERN / COMPRESS_MODES - kept as
# separate constants rather than an import, since each is a standalone
# opt-nesi-bin command.
DEFAULT_PATTERN = "*_final"
COMPRESS_MODES = ("auto", "always", "never")

TOP_USAGE = f"""usage: {PROGNAME} <subcommand> [OPTIONS]

subcommands:
  add        create or update a managed archiving entry
  remove     remove one entry (--folder), or all of them (--all)
  status     show archived-file counts and last activity per entry

Run "{PROGNAME} <subcommand> --help" for subcommand-specific options.
"""

# Recognised but not built yet - see design discussion. Cheapest sketch:
# comment the managed line out in place (leading "#") rather than a
# separate saved-config file, so re-enabling doesn't need folder/bucket/
# pattern re-supplied. Kept separate from SUBCOMMANDS so invoking these
# gives a clear "not built yet" message instead of "unknown subcommand".
PLANNED_SUBCOMMANDS = ("enable", "disable")


# --- entry identity ---------------------------------------------------------
# One scrontab line per (folder, pattern) pair. The marker embeds a short
# hash of that pair so multiple entries can coexist and be addressed
# individually, rather than the old single-MARKER "one entry total" model.

def entry_id(folder, pattern):
    return hashlib.sha256(f"{folder}:{pattern}".encode()).hexdigest()[:8]


def marker_for(entry_id_):
    return f"{MARKER_PREFIX}:{entry_id_}"


def validate(folder, bucket):
    """
    folder exists/writable, Freezer access works, 775 perms look sane.
    Raises ValueError with a human-readable reason on failure.
    SPEC "How it works" 3.2 step 2.
    """
    path = Path(folder)
    if not path.is_dir() or not os.access(path, os.W_OK):
        raise ValueError(f"{folder} does not exist or isn't writable")
    # TODO: not started - a lightweight `s3cmd ls` against bucket to
    # confirm Freezer access, and a check that group perms are 775-ish
    # (warn, don't fail - SPEC Requirements says permissions are
    # pre-existing, not managed here). Neither is implemented, so
    # `archive_tool` currently only verifies the folder itself.


# --- scrontab editing --------------------------------------------------
# Always read-modify-write against the existing table, never a blind
# overwrite - SPEC "How it works" 3.2 step 3.

def read_scrontab():
    # A nonzero exit is treated as "no table yet" (the normal case for a
    # first-time add). NOT verified against real scrontab behavior:
    # this can't currently tell that apart from a genuine read failure,
    # which would silently be treated as an empty table too - see SPEC
    # Open Questions.
    proc = subprocess.run(["scrontab", "-l"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def write_scrontab(content):
    subprocess.run(["scrontab", "-"], input=content, text=True, check=True)


def build_entry(schedule, folder, bucket, pattern, compress_mode):
    # pattern (e.g. "*_final") must be quoted - this line is shell-invoked
    # by scrontab, and an unquoted glob would expand against $PWD first.
    marker = marker_for(entry_id(folder, pattern))
    return (
        f"{schedule} runner_archive --folder {folder} --bucket {bucket} "
        f"--pattern '{pattern}' --compress {compress_mode} {marker}\n"
    )


def add_or_update_entry(schedule, folder, bucket, pattern, compress_mode):
    """Replace this entry's own line if present; every other line - including other managed entries - untouched."""
    marker = marker_for(entry_id(folder, pattern))
    lines = [l for l in read_scrontab().splitlines(keepends=True) if marker not in l]
    lines.append(build_entry(schedule, folder, bucket, pattern, compress_mode))
    write_scrontab("".join(lines))


def remove_entry(folder, pattern):
    """Remove one entry (identified by folder+pattern); every other line - including other managed entries - untouched."""
    marker = marker_for(entry_id(folder, pattern))
    lines = [l for l in read_scrontab().splitlines(keepends=True) if marker not in l]
    write_scrontab("".join(lines))


def remove_all_entries():
    """Remove every entry this tool manages, regardless of folder/pattern. Unrelated scrontab lines untouched."""
    lines = [l for l in read_scrontab().splitlines(keepends=True) if MARKER_PREFIX not in l]
    write_scrontab("".join(lines))


# --- status ------------------------------------------------------------
# Parses managed lines back out of the table. Only understands the exact
# shape build_entry() produces - anything hand-edited won't match.

ENTRY_RE = re.compile(
    r"^(?P<schedule>(?:\S+ ){4}\S+) runner_archive "
    r"--folder (?P<folder>\S+) --bucket (?P<bucket>\S+) "
    r"--pattern '(?P<pattern>[^']*)' --compress (?P<compress>\S+) "
    + re.escape(MARKER_PREFIX) + r":(?P<id>[0-9a-f]+)\s*$"
)


def list_entries():
    entries = []
    for line in read_scrontab().splitlines():
        m = ENTRY_RE.match(line.strip())
        if m:
            entries.append(m.groupdict())
    return entries


def count_metadata_entries(metadata_path):
    """Line count in the metadata file - see runner_archive.py's load_metadata()."""
    if not metadata_path.exists():
        return 0
    with metadata_path.open() as fh:
        return sum(1 for line in fh if line.strip())


def last_activity(log_path):
    if not log_path.exists():
        return "never"
    return datetime.fromtimestamp(log_path.stat().st_mtime).isoformat(timespec="seconds")


def format_status(entry):
    state_dir = Path(entry["folder"]) / ".freezer"
    archived_count = count_metadata_entries(state_dir / "metadata.jsonl")
    return (
        f"[{entry['id']}] {entry['folder']}\n"
        f"    bucket:    {entry['bucket']}\n"
        f"    pattern:   {entry['pattern']!r}\n"
        f"    schedule:  {entry['schedule']}\n"
        f"    compress:  {entry['compress']}\n"
        f"    archived:  {archived_count} file(s)\n"
        f"    last run:  {last_activity(state_dir / 'archive.log')}"
    )


# --- add -------------------------------------------------------------

ADD_USAGE = f"""usage: {PROGNAME} add [OPTIONS]

  -f, --folder PATH     nobackup folder to watch (required)
  -b, --bucket NAME      Freezer bucket to archive into (required)
  -p, --pattern GLOB     top-level folder glob to archive (default: "{DEFAULT_PATTERN}")
  -c, --compress MODE    auto (default), always, or never
  -s, --schedule CRON    scrontab schedule (default: "{DEFAULT_SCHEDULE}")
  -h, --help
"""


def parse_add_args(argv):
    try:
        opts, _args = getopt.getopt(
            argv, "f:b:p:c:s:h",
            ["folder=", "bucket=", "pattern=", "compress=", "schedule=", "help"],
        )
    except getopt.GetoptError as e:
        print(f"{PROGNAME} add: {e}", file=sys.stderr)
        print(ADD_USAGE, file=sys.stderr)
        sys.exit(2)

    opts = dict(opts)
    if "-h" in opts or "--help" in opts:
        print(ADD_USAGE)
        sys.exit(0)

    folder = opts.get("-f") or opts.get("--folder")
    bucket = opts.get("-b") or opts.get("--bucket")
    if not folder or not bucket:
        print(f"{PROGNAME} add: --folder and --bucket are required", file=sys.stderr)
        print(ADD_USAGE, file=sys.stderr)
        sys.exit(2)

    compress_mode = opts.get("-c") or opts.get("--compress") or "auto"
    if compress_mode not in COMPRESS_MODES:
        print(f"{PROGNAME} add: --compress must be one of {COMPRESS_MODES}", file=sys.stderr)
        sys.exit(2)

    pattern = opts.get("-p") or opts.get("--pattern") or DEFAULT_PATTERN
    schedule = opts.get("-s") or opts.get("--schedule") or DEFAULT_SCHEDULE

    return folder, bucket, pattern, compress_mode, schedule


def cmd_add(argv):
    folder, bucket, pattern, compress_mode, schedule = parse_add_args(argv)
    validate(folder, bucket)
    add_or_update_entry(schedule, folder, bucket, pattern, compress_mode)

    print(f"added: {build_entry(schedule, folder, bucket, pattern, compress_mode).strip()}")
    print(f"log:       {folder}/.freezer/archive.log")
    print(f"metadata:  {folder}/.freezer/metadata.jsonl")
    print(f"to remove: {PROGNAME} remove --folder {folder} --pattern '{pattern}'")
    return 0


# --- remove --------------------------------------------------------------

REMOVE_USAGE = f"""usage: {PROGNAME} remove [OPTIONS]

  -f, --folder PATH     entry to remove (required unless --all)
  -p, --pattern GLOB     pattern the entry was added with (default: "{DEFAULT_PATTERN}")
  -a, --all               remove every entry this tool manages
  -h, --help
"""


def parse_remove_args(argv):
    try:
        opts, _args = getopt.getopt(argv, "f:p:ah", ["folder=", "pattern=", "all", "help"])
    except getopt.GetoptError as e:
        print(f"{PROGNAME} remove: {e}", file=sys.stderr)
        print(REMOVE_USAGE, file=sys.stderr)
        sys.exit(2)

    opts = dict(opts)
    if "-h" in opts or "--help" in opts:
        print(REMOVE_USAGE)
        sys.exit(0)

    remove_all = "-a" in opts or "--all" in opts
    folder = opts.get("-f") or opts.get("--folder")
    pattern = opts.get("-p") or opts.get("--pattern") or DEFAULT_PATTERN

    if not remove_all and not folder:
        print(f"{PROGNAME} remove: --folder is required unless --all", file=sys.stderr)
        print(REMOVE_USAGE, file=sys.stderr)
        sys.exit(2)

    return folder, pattern, remove_all


def cmd_remove(argv):
    folder, pattern, remove_all = parse_remove_args(argv)
    if remove_all:
        remove_all_entries()
        print("removed all managed entries")
    else:
        remove_entry(folder, pattern)
        print(f"removed entry for {folder} (pattern {pattern!r})")
    return 0


# --- status ----------------------------------------------------------------

STATUS_USAGE = f"""usage: {PROGNAME} status [OPTIONS]

  -f, --folder PATH     show only this entry (default: show all managed entries)
  -p, --pattern GLOB     pattern the entry was added with (default: "{DEFAULT_PATTERN}")
  -h, --help
"""


def parse_status_args(argv):
    try:
        opts, _args = getopt.getopt(argv, "f:p:h", ["folder=", "pattern=", "help"])
    except getopt.GetoptError as e:
        print(f"{PROGNAME} status: {e}", file=sys.stderr)
        print(STATUS_USAGE, file=sys.stderr)
        sys.exit(2)

    opts = dict(opts)
    if "-h" in opts or "--help" in opts:
        print(STATUS_USAGE)
        sys.exit(0)

    folder = opts.get("-f") or opts.get("--folder")
    pattern = opts.get("-p") or opts.get("--pattern") or DEFAULT_PATTERN
    return folder, pattern


def cmd_status(argv):
    folder, pattern = parse_status_args(argv)
    entries = list_entries()

    if folder:
        wanted = entry_id(folder, pattern)
        entries = [e for e in entries if e["id"] == wanted]
        if not entries:
            print(f"{PROGNAME}: no managed entry for {folder!r} (pattern {pattern!r})", file=sys.stderr)
            return 1

    if not entries:
        print("no managed entries")
        return 0

    print("\n".join(format_status(e) for e in entries))
    return 0


# --- main --------------------------------------------------------------

SUBCOMMANDS = {
    "add": cmd_add,
    "remove": cmd_remove,
    "status": cmd_status,
}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(TOP_USAGE)
        return 0

    subcommand, rest = argv[0], argv[1:]

    if subcommand in PLANNED_SUBCOMMANDS:
        print(f"{PROGNAME}: {subcommand!r} is planned but not implemented yet", file=sys.stderr)
        return 2

    if subcommand not in SUBCOMMANDS:
        print(f"{PROGNAME}: unknown subcommand {subcommand!r}", file=sys.stderr)
        print(TOP_USAGE, file=sys.stderr)
        return 2

    return SUBCOMMANDS[subcommand](rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
