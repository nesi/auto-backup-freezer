#!/bin/python
"""
archive_tool - manage the scrontab entries that run runner_archive.

An entry's identity is a short hash of its absolute pattern, embedded in its
scrontab marker: different patterns never collide, and re-running `add` for
the same pattern updates that entry in place.
"""

import fnmatch
import getopt
import gzip
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace

PROGNAME = "archive_tool"
MARKER_PREFIX = f"# managed-by-{PROGNAME}"
JOB_NAME_PREFIX = f"{PROGNAME}-"
DEFAULT_SCHEDULE = "0 2 * * *"
# Mirrored from runner_archive.py (independent commands, so not imported)
STATE_DIR_NAME = ".freezer"
COMPRESS_MODES = ("auto", "always", "never")
DEFAULT_COMPRESS_MODE = "auto"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_RETENTION_DAYS = 730
NOBACKUP_ROOT = "/nesi/nobackup"
SHARED_ROOTS = ("/nesi/project", "/nesi/nobackup")  # where a non-group-writable base dir gets a warning
# The auto-cleaner's per-project deletion candidate lists. Readable on login03
# only, not from compute nodes - so `status` shows it, runner_archive can't use it.
AUTOCLEANER_LIST_DIR = "/search/autocleaner/filelists/current"

ID_LENGTH = 6
ID_RE = re.compile(r"^[0-9a-f]{%d}$" % ID_LENGTH)

TOP_USAGE = f"""usage: {PROGNAME} <subcommand> [OPTIONS]

subcommands:
  add        create or update a managed archiving entry
  remove     remove one entry (--pattern), or all of them (--all)
  status     show archived-file counts and last activity per entry

Run "{PROGNAME} <subcommand> --help" for subcommand-specific options.
"""


def die(msg, usage=None):
    print(f"{PROGNAME}: {msg}", file=sys.stderr)
    if usage:
        print(usage, file=sys.stderr)
    sys.exit(2)


def getopts(argv, subcommand, options, usage):
    """
    getopt over [(short or None, long)] (a trailing "=" takes a value), each
    long option folded onto its short one. Handles --help, bad flags and stray
    positionals (getopt stops at the first, silently dropping every flag after).
    """
    try:
        pairs, rest = getopt.getopt(
            argv, "".join(s + ":" * l.endswith("=") for s, l in options if s), [l for _, l in options]
        )
    except getopt.GetoptError as e:
        die(f"{subcommand}: {e}", usage)
    alias = {f"--{l.rstrip('=')}": f"-{s}" for s, l in options if s}
    opts = {alias.get(k, k): v for k, v in pairs}
    if "-h" in opts:
        print(usage)
        sys.exit(0)
    if rest:
        die(f"{subcommand}: unexpected argument(s): {' '.join(rest)}", usage)
    return opts


def nonnegative_int(raw, flag):
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        die(f"--{flag} must be an integer, got {raw!r}")
    if value < 0:
        die(f"--{flag} must be >= 0")
    return value


# --- entry identity -----------------------------------------------------------

def entry_id(pattern):
    return hashlib.sha256(pattern.encode()).hexdigest()[:ID_LENGTH]


def marker_for(id_):
    return f"{MARKER_PREFIX}:{id_}"


def job_name_for(id_):
    return f"{JOB_NAME_PREFIX}{id_}"


def directive_for(id_):
    """Written above each entry's cron line, so its Slurm job can be found in squeue."""
    return f"#SCRON --job-name={job_name_for(id_)}\n"


def _is_managed_line(line, id_=None):
    """An entry's cron line (by marker) or #SCRON directive - for one entry id, or any if id is None."""
    if id_ is None:
        return MARKER_PREFIX in line or line.strip().startswith(f"#SCRON --job-name={JOB_NAME_PREFIX}")
    return marker_for(id_) in line or line.strip() == directive_for(id_).strip()


def resolve_to_id(raw):
    """A --pattern argument, given as either an entry id or a pattern."""
    return raw.lower() if ID_RE.match(raw.lower()) else entry_id(os.path.abspath(raw))


def split_pattern(pattern):
    path = Path(pattern)
    return path.parent, path.name


def bucket_uri(bucket, key=""):
    """s3://<bucket>/<key>, whether or not --bucket was typed with its own "s3://"."""
    if bucket.lower().startswith("s3://"):
        bucket = bucket[len("s3://"):]
    return f"s3://{bucket}/{key}"


def retention_of(entry):
    return int(entry["retention_days"]) if entry.get("retention_days") else None


def validate(pattern, bucket):
    """Raises ValueError if the pattern, base dir or bucket won't work; warns on non-group-writable shared dirs."""
    if "'" in pattern:
        raise ValueError(f"pattern must not contain a single quote: {pattern!r}")
    base_dir, _ = split_pattern(pattern)
    if not base_dir.is_dir() or not os.access(base_dir, os.W_OK):
        raise ValueError(f"{base_dir} does not exist or isn't writable")
    try:
        proc = subprocess.run(["s3cmd", "ls", bucket_uri(bucket)], capture_output=True, text=True)
    except FileNotFoundError:
        raise ValueError("s3cmd not found.")
    if proc.returncode == 78:
        raise ValueError("s3cmd is not configured. "
                         "See: https://docs.nesi.org.nz/Storage/Long_Term_Storage/Configuring_S3cmd/")
    if proc.returncode:
        stderr = proc.stderr.strip()
        reason = stderr.splitlines()[-1] if stderr else f"s3cmd exited {proc.returncode}"
        raise ValueError(f"could not access Freezer bucket {bucket!r}: {reason}")
    shared = any(Path(os.path.realpath(base_dir)).is_relative_to(root) for root in SHARED_ROOTS)
    if shared and not base_dir.stat().st_mode & stat.S_IWGRP:
        print(f"warning: {base_dir} is not group-writable, other collaborators "
              "may not be able to write into it", file=sys.stderr)


# --- scrontab -----------------------------------------------------------------

def read_scrontab():
    return subprocess.run(["scrontab", "-l"], capture_output=True, text=True).stdout


def write_scrontab(content):
    subprocess.run(["scrontab", "-"], input=content, text=True, check=True)


def runner_archive_flags(compress_mode=None, retention_days=None, log_level=None, mail_user=None):
    """runner_archive flags; None/empty ones are omitted, leaving runner_archive's default."""
    flags = []
    for flag, value in (("--compress", compress_mode), ("--retention-days", retention_days),
                        ("--log-level", log_level), ("--mail-user", mail_user)):
        if value is not None and value != "":
            flags += [flag, str(value)]
    return flags


def manual_command(entry, flag):
    """The runner command to run by hand for a step scheduled runs only warn about (--delete-expired, --overwrite)."""
    return shlex.join(["runner_archive", "--pattern", entry["pattern"], "--bucket", entry["bucket"],
                       *runner_archive_flags(entry.get("compress"), retention_of(entry)), flag])


def build_entry(schedule, pattern, bucket, compress_mode=None, retention_days=None, log_level=None, mail_user=None):
    """The entry's cron line (add_or_update_entry() writes directive_for() above it)."""
    flags = " ".join(runner_archive_flags(compress_mode, retention_days, log_level, mail_user))
    return (f"{schedule} runner_archive --pattern '{pattern}' --bucket {bucket} "
            f"{flags + ' ' if flags else ''}{marker_for(entry_id(pattern))}\n")


ENTRY_RE = re.compile(
    r"^(?P<schedule>(?:\S+ ){4}\S+) runner_archive "
    r"--pattern '(?P<pattern>[^']*)' --bucket (?P<bucket>\S+) "
    r"(?:--compress (?P<compress>\S+) )?"
    r"(?:--retention-days (?P<retention_days>\d+) )?"
    r"(?:--log-level (?P<log_level>\S+) )?"
    r"(?:--mail-user (?P<mail_user>\S+) )?"
    + re.escape(MARKER_PREFIX) + r":(?P<id>[0-9a-f]+)\s*$"
)


def list_entries():
    """Managed entries, parsed back out of the table (plus their raw "line")."""
    return [{**m.groupdict(), "line": line.strip()}
            for line in read_scrontab().splitlines() if (m := ENTRY_RE.match(line.strip()))]


def check_id_collision(pattern):
    """Raises ValueError if `pattern`'s id is already taken by a *different* pattern (astronomically unlikely)."""
    for entry in list_entries():
        if entry["id"] == entry_id(pattern) and entry["pattern"] != pattern:
            raise ValueError(f"id {entry['id']} for pattern {pattern!r} collides with the existing entry for "
                             f"{entry['pattern']!r} - rename one of the folders so the patterns differ")


def add_or_update_entry(schedule, pattern, bucket, compress_mode=None, retention_days=None,
                        log_level=None, mail_user=None):
    """Replace this entry's own lines (directive + cron line), if any, leaving everything else as-is."""
    check_id_collision(pattern)
    id_ = entry_id(pattern)
    lines = [l for l in read_scrontab().splitlines(keepends=True) if not _is_managed_line(l, id_)]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"  # else the directive would be glued onto the last line
    lines += [directive_for(id_),
              build_entry(schedule, pattern, bucket, compress_mode, retention_days, log_level, mail_user)]
    write_scrontab("".join(lines))


def remove_entry(id_):
    write_scrontab("".join(l for l in read_scrontab().splitlines(keepends=True) if not _is_managed_line(l, id_)))


def remove_all_entries():
    """Remove every managed entry. Unrelated lines are left alone."""
    write_scrontab("".join(l for l in read_scrontab().splitlines(keepends=True) if not _is_managed_line(l)))


# --- status -------------------------------------------------------------------

def scheduled_runs():
    """
    {job name: (state, start)} from squeue, or None if squeue can't be run. An
    entry's next occurrence is a PENDING job whose start time is its next run
    (Slurm's own clock and timezone - no cron re-implementation needed).
    """
    try:
        proc = subprocess.run(["squeue", "--me", "--noheader", "--format=%j|%T|%S"], capture_output=True,
                              text=True, env={**os.environ, "SLURM_TIME_FORMAT": "%Y-%m-%d %H:%M"})
    except FileNotFoundError:
        return None
    if proc.returncode:
        return None
    runs = {}
    for parts in (line.strip().split("|") for line in proc.stdout.splitlines()):
        if len(parts) == 3:
            runs.setdefault(parts[0], (parts[1], parts[2]))
    return runs


def format_next_run(entry, runs):
    if runs is None:
        return "unknown (squeue unavailable)"
    job = runs.get(job_name_for(entry["id"]))
    if job is None:
        return "not scheduled (check `scrontab -l` for a #DISABLED line)"
    state, start = job
    return f"running now (started {start})" if state == "RUNNING" else start


def last_activity(log_path):
    if not log_path.exists():
        return "never"
    return datetime.fromtimestamp(log_path.stat().st_mtime).isoformat(timespec="seconds")


def read_records(metadata_path):
    if not metadata_path.exists():
        return []
    records = []
    for line in metadata_path.read_text().splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"warning: skipping unparseable metadata line in {metadata_path}: {line.strip()!r}",
                      file=sys.stderr)
    return records


def file_records(metadata_path):
    """Per-file archive records (not `deleted` events)."""
    return [r for r in read_records(metadata_path) if "file" in r]


def list_bucket_dates(bucket):
    """{object name: LastModified date}, or None on failure. Mirrors runner_archive's list_bucket_objects()."""
    proc = subprocess.run(["s3cmd", "ls", "-l", "-H", bucket_uri(bucket)], capture_output=True, text=True)
    if proc.returncode:
        return None
    objects = {}
    for parts in (line.split() for line in proc.stdout.splitlines()):
        try:
            objects[parts[-1].rsplit("/", 1)[-1]] = datetime.strptime(parts[0], "%Y-%m-%d").date()
        except (IndexError, ValueError):
            pass
    return objects


def retention_status(metadata_path, bucket, retention_days, bucket_cache=None):
    """
    (expired, deleted): expired is sorted [(tar_name, age_days)] still in
    Freezer, or None if retention is off or the listing failed ("couldn't
    check", not "nothing"). deleted counts recorded deletions plus tars already
    gone by hand. `bucket_cache` shares one listing across entries.
    """
    records = read_records(metadata_path)
    deleted = {r["tar_name"] for r in records if r.get("event") == "deleted"}
    live = {r["tar_name"] for r in records if "file" in r} - deleted
    if retention_days <= 0:
        return None, len(deleted)
    if not live:
        return [], len(deleted)

    cache = {} if bucket_cache is None else bucket_cache
    key = bucket_uri(bucket)
    if key not in cache:
        cache[key] = list_bucket_dates(bucket)
    objects = cache[key]
    if objects is None:
        return None, len(deleted)

    # Mirrors runner_archive's delete_expired_archives() - keep in sync.
    today = datetime.now().date()
    ages = {t: (today - objects[t]).days for t in live if t in objects}
    expired = sorted((t, age) for t, age in ages.items() if age >= retention_days)
    return expired, len(deleted) + len(live - ages.keys())


def pending_on_cleaner_list(pattern, metadata_path):
    """
    How many of the entry's pending (unarchived) files are on the auto-cleaner's
    deletion list - non-zero means archiving is stuck. None if the base dir
    isn't under nobackup or the list isn't readable here.
    """
    base_dir, glob_expr = split_pattern(pattern)
    real_base, root = Path(os.path.realpath(base_dir)), os.path.realpath(NOBACKUP_ROOT)
    if not real_base.is_relative_to(root) or real_base == Path(root):
        return None
    list_path = Path(AUTOCLEANER_LIST_DIR) / f"{real_base.relative_to(root).parts[0]}.gz"
    archived = {(r["folder"], r["file"]) for r in file_records(metadata_path)}
    prefix = f"{real_base}{os.sep}"
    count = 0
    try:
        with gzip.open(list_path, "rt", errors="replace") as fh:
            for path in (line.rstrip("\n") for line in fh):
                if not path.startswith(prefix):
                    continue
                folder, _, rel = path[len(prefix):].partition(os.sep)
                # the list can be a week stale - skip files archived or deleted since
                count += bool(rel and folder != STATE_DIR_NAME and fnmatch.fnmatchcase(folder, glob_expr)
                              and (folder, rel) not in archived and os.path.lexists(path))
    except (OSError, EOFError):
        return None
    return count


def format_status(entry, bucket_cache=None, runs=None):
    """`runs` is a scheduled_runs() result shared across entries (None: squeue unavailable)."""
    state_dir = split_pattern(entry["pattern"])[0] / STATE_DIR_NAME
    metadata_path = state_dir / "metadata.jsonl"
    retention_days = DEFAULT_RETENTION_DAYS if retention_of(entry) is None else retention_of(entry)
    lines = [
        f"[{entry['id']}] {entry['pattern']}",
        f"    bucket:      {entry['bucket']}",
        f"    schedule:    {entry['schedule']}",
        f"    compress:    {entry.get('compress') or DEFAULT_COMPRESS_MODE}",
        f"    log level:   {entry.get('log_level') or DEFAULT_LOG_LEVEL}",
        f"    mail:        {entry.get('mail_user') or 'not configured'}",
        f"    archived:    {len(file_records(metadata_path))} file(s)",
    ]

    expired, deleted = retention_status(metadata_path, entry["bucket"], retention_days, bucket_cache)
    if retention_days <= 0:
        lines.append(f"    retention:   disabled, {deleted} already deleted")
    else:
        lines.append(f"    retention:   {retention_days} day(s)")
        if expired is None:
            lines.append(f"    expired:     unknown (Freezer listing unavailable), {deleted} already deleted")
        elif expired:
            lines += [f"    expired:     {len(expired)} past retention, {deleted} already deleted",
                      f"                 to delete: {manual_command(entry, '--delete-expired')}",
                      "                 (add --dry-run first to preview)"]
        else:
            lines.append(f"    expired:     0, {deleted} already deleted")

    on_list = pending_on_cleaner_list(entry["pattern"], metadata_path)
    if on_list:
        lines.append(f"    cleaner:     {on_list} pending file(s) on nobackup's auto-delete list "
                     "- archiving may be stuck, check archive.log")
    elif on_list == 0:
        lines.append("    cleaner:     no pending files on nobackup's auto-delete list")

    lines += [f"    last run:    {last_activity(state_dir / 'archive.log')}",
              f"    next run:    {format_next_run(entry, runs)}"]
    return "\n".join(lines)


# --- add ----------------------------------------------------------------------

ADD_USAGE = f"""usage: {PROGNAME} add [OPTIONS]

  -p, --pattern "GLOB"   folder glob to watch (required, quote it)
  -b, --bucket NAME      Freezer bucket to archive into (required)
  -c, --compress MODE    auto (default), always, or never
  -s, --schedule CRON    scrontab schedule (default: "{DEFAULT_SCHEDULE}")
  -r, --retention-days N warn once a tar in Freezer is this old (default 730, 0 disables);
                         `status` shows the command to delete them
  -l, --log-level LEVEL  DEBUG, INFO (default), WARNING, ERROR
  -m, --mail-user EMAIL  email each run's output to EMAIL (STUB)
  -n, --dry-run          don't change anything, just show what would happen
      --no-run           don't run the archive straight away
  -y, --yes              don't prompt for confirmation
  -h, --help             show this message
"""


def parse_add_args(argv):
    opts = getopts(argv, "add", [
        ("p", "pattern="), ("b", "bucket="), ("c", "compress="), ("s", "schedule="), ("r", "retention-days="),
        ("l", "log-level="), ("m", "mail-user="), ("n", "dry-run"), (None, "no-run"), ("y", "yes"), ("h", "help"),
    ], ADD_USAGE)
    if not opts.get("-p") or not opts.get("-b"):
        die("--pattern and --bucket are required", ADD_USAGE)
    args = SimpleNamespace(
        pattern=os.path.abspath(opts["-p"]),
        bucket=opts["-b"],
        compress_mode=opts.get("-c") or None,
        schedule=opts.get("-s") or DEFAULT_SCHEDULE,
        retention_days=nonnegative_int(opts.get("-r"), "retention-days"),
        log_level=opts["-l"].upper() if opts.get("-l") else None,  # any case, like runner_archive
        mail_user=opts.get("-m") or None,
        dry_run="-n" in opts,
        no_run="--no-run" in opts,
        yes="-y" in opts,
    )
    if args.compress_mode not in (None, *COMPRESS_MODES):
        die(f"--compress must be one of {COMPRESS_MODES}")
    if args.log_level not in (None, *LOG_LEVELS):
        die(f"--log-level must be one of {LOG_LEVELS}")
    if args.mail_user and any(c.isspace() for c in args.mail_user):
        die(f"email cannot contain whitespace, got {args.mail_user!r}")
    return args


def run_archive(pattern, bucket, compress_mode, retention_days, dry_run, log_level=None):
    """Run what the entry would run (without --mail-user: the output's shown here), with --dry-run if previewing."""
    argv = ["runner_archive", "--pattern", pattern, "--bucket", bucket,
            *runner_archive_flags(compress_mode, retention_days, log_level), *(["--dry-run"] if dry_run else [])]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if output := (proc.stdout + proc.stderr).strip():
        print(output)


def _confirm(subcommand, lines, question):
    """Print `lines`, ask `question`. Non-interactive: refuse rather than hang (a script missing --yes fails loudly)."""
    print("\n".join(lines))
    if not sys.stdin.isatty():
        print(f"{PROGNAME}: {subcommand}: refusing to proceed without confirmation "
              "in a non-interactive context, pass --yes to confirm", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cmd_add(argv):
    args = parse_add_args(argv)
    try:
        validate(args.pattern, args.bucket)
    except ValueError as e:
        print(f"{PROGNAME}: {e}", file=sys.stderr)
        return 1

    settings = (args.compress_mode, args.retention_days, args.log_level, args.mail_user)
    run = partial(run_archive, args.pattern, args.bucket, args.compress_mode, args.retention_days,
                  log_level=args.log_level)
    entry = build_entry(args.schedule, args.pattern, args.bucket, *settings).strip()
    existing = next((e for e in list_entries() if e["id"] == entry_id(args.pattern)), None)

    if args.dry_run:
        print(f"[dry-run] {'would update' if existing else 'would add'}: {entry}")
        run(dry_run=True)
        return 0
    if existing and not args.yes and not _confirm(
            "add", ["an entry already exists for this pattern:",
                    f"  current:  {existing['line']}", f"  new:      {entry}"], "overwrite?"):
        print(f"{PROGNAME}: aborted. No changes made.", file=sys.stderr)
        return 1

    add_or_update_entry(args.schedule, args.pattern, args.bucket, *settings)
    state_dir = split_pattern(args.pattern)[0] / STATE_DIR_NAME
    print(f"added: {entry}\nlog:       {state_dir}/archive.log\nmetadata:  {state_dir}/metadata.jsonl")
    if not args.no_run:
        print("running the archive now (pass --no-run to skip this and just wait for the schedule)...")
        run(dry_run=False)
    return 0


# --- remove -------------------------------------------------------------------

REMOVE_USAGE = f"""usage: {PROGNAME} remove [OPTIONS]

  -p, --pattern GLOB|ID     entry to remove
  -a, --all                 remove every entry this tool manages
  -y, --yes                 don't prompt for confirmation
  -h, --help                show this message
"""


def parse_remove_args(argv):
    opts = getopts(argv, "remove", [("p", "pattern="), ("a", "all"), ("y", "yes"), ("h", "help")], REMOVE_USAGE)
    if "-a" not in opts and not opts.get("-p"):
        die("--pattern or --all required", REMOVE_USAGE)
    return (resolve_to_id(opts["-p"]) if opts.get("-p") else None), "-a" in opts, "-y" in opts


def cmd_remove(argv):
    id_, remove_all, yes = parse_remove_args(argv)
    entries = [e for e in list_entries() if remove_all or e["id"] == id_]
    if not entries:
        if remove_all:
            print("no entries to remove")
            return 0
        print(f"{PROGNAME}: no entry for {id_!r}, see `{PROGNAME} status` for full list.", file=sys.stderr)
        return 1
    listing = [f"  [{e['id']}] {e['pattern']}" for e in entries]
    header = "this will remove ALL entries:" if remove_all else "this will remove:"
    if not yes and not _confirm("remove", [header, *listing], "remove all?" if remove_all else "remove?"):
        print(f"{PROGNAME}: aborted: nothing removed", file=sys.stderr)
        return 1
    remove_all_entries() if remove_all else remove_entry(id_)
    print(f"removed {len(entries)} entry(s)" if remove_all else f"removed entry [{id_}] {entries[0]['pattern']}")
    return 0


# --- status -------------------------------------------------------------------

STATUS_USAGE = f"""usage: {PROGNAME} status [OPTIONS]

  -p, --pattern GLOB|ID     show only this entry
  -h, --help                show this message
"""


def parse_status_args(argv):
    opts = getopts(argv, "status", [("p", "pattern="), ("h", "help")], STATUS_USAGE)
    return resolve_to_id(opts["-p"]) if opts.get("-p") else None


def cmd_status(argv):
    wanted = parse_status_args(argv)
    entries = [e for e in list_entries() if not wanted or e["id"] == wanted]
    if not entries:
        if wanted:
            print(f"{PROGNAME}: no managed entry for {wanted!r}", file=sys.stderr)
            return 1
        print("no managed entries")
        return 0
    bucket_cache, runs = {}, scheduled_runs()  # one listing per bucket, one squeue call for all entries
    print("\n".join(format_status(e, bucket_cache, runs) for e in entries))
    return 0


SUBCOMMANDS = {"add": cmd_add, "remove": cmd_remove, "status": cmd_status}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(TOP_USAGE)
        return 0
    if argv[0] not in SUBCOMMANDS:
        print(f"{PROGNAME}: unknown subcommand {argv[0]!r}\n{TOP_USAGE}", file=sys.stderr)
        return 2
    return SUBCOMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
