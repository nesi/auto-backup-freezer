#!/bin/python
"""Archives matching folders to Freezer. Runs unattended from a scrontab entry installed by `archive_tool add`."""

import fcntl
import getopt
import json
import logging
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from itertools import count
from pathlib import Path
from typing import Optional

PROGNAME = "runner_archive"
STATE_DIR_NAME = ".freezer"  # metadata/lock/log dir, never archived
# nobackup's auto-cleaner deletes a file once its atime and ctime are both >90
# days old, having listed it (and emailed its owner) at ~76. 60 keeps pending
# files off that list, with margin for its fortnightly cycle.
TOUCH_THRESHOLD_DAYS = 60
TOUCH_BATCH_SIZE = 500  # paths per `touch` call
NOBACKUP_ROOT = "/nesi/nobackup"
DEFAULT_RETENTION_DAYS = 730
COMPRESS_MODES = ("auto", "always", "never")
COMPRESSED_FRACTION_THRESHOLD = 0.9  # 'auto' won't compress if at least this fraction (by bytes) already is
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LOG_LEVEL = "INFO"

USAGE = f"""usage: {PROGNAME} [OPTIONS]

  -p, --pattern "GLOB"      folder glob to archive (required, quote it)
  -b, --bucket NAME         Freezer bucket to archive into (required)
  -c, --compress MODE       auto (default), always, or never
  -r, --retention-days N    warn once a tar in Freezer is this old (default 730, 0 disables)
  -d, --delete-expired      delete tars past --retention-days (otherwise only warned about)
  -o, --overwrite           re-archive files changed since archiving, deleting the tar they superseded
  -n, --dry-run             don't change anything, just log what would happen
  -l, --log-level LEVEL     console log level: DEBUG, INFO (default), WARNING, ERROR
  -m, --mail-user EMAIL     email this run's output to EMAIL (STUB)
  -h, --help                show this message
"""

# (short, long) - a trailing "=" means the option takes a value
OPTIONS = [
    ("p", "pattern="), ("b", "bucket="), ("c", "compress="), ("r", "retention-days="),
    ("d", "delete-expired"), ("o", "overwrite"), ("n", "dry-run"), ("l", "log-level="),
    ("m", "mail-user="), ("h", "help"),
]

# Magic bytes of already-compressed formats (BAM is BGZF, so shares gzip's).
MAGIC_BYTES = [b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd", b"PK\x03\x04", b"CRAM"]

log = logging.getLogger(PROGNAME)


def setup_logging(log_path, log_level):
    """archive.log gets everything, the console only --log-level and up. Call once per process."""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.DEBUG)
    for handler, level in ((logging.FileHandler(log_path), logging.DEBUG),
                           (logging.StreamHandler(), getattr(logging, log_level))):
        handler.setLevel(level)
        handler.setFormatter(formatter)
        log.addHandler(handler)


def usage_error(msg):
    print(f"{PROGNAME}: {msg}\n{USAGE}", file=sys.stderr)
    sys.exit(2)


def nonnegative_int(raw, flag, default):
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        usage_error(f"--{flag} must be an integer, got {raw!r}")
    if value < 0:
        usage_error(f"--{flag} must be >= 0")
    return value


@dataclass
class Args:
    pattern: str
    bucket: str
    compress_mode: str
    retention_days: int
    delete_expired: bool
    overwrite: bool
    dry_run: bool
    log_level: str
    mail_user: Optional[str]


def parse_args(argv):
    try:
        pairs, rest = getopt.getopt(
            argv, "".join(s + ":" * l.endswith("=") for s, l in OPTIONS), [l for _, l in OPTIONS]
        )
    except getopt.GetoptError as e:
        usage_error(str(e))
    alias = {f"--{l.rstrip('=')}": f"-{s}" for s, l in OPTIONS}
    opts = {alias.get(k, k): v for k, v in pairs}
    if "-h" in opts:
        print(USAGE)
        sys.exit(0)
    if rest:  # getopt stops at the first positional, silently dropping every flag after it
        usage_error(f"unexpected argument(s): {' '.join(rest)}")

    args = Args(
        pattern=opts.get("-p"),
        bucket=opts.get("-b"),
        compress_mode=opts.get("-c") or "auto",
        retention_days=nonnegative_int(opts.get("-r"), "retention-days", DEFAULT_RETENTION_DAYS),
        delete_expired="-d" in opts,
        overwrite="-o" in opts,
        dry_run="-n" in opts,
        log_level=(opts.get("-l") or DEFAULT_LOG_LEVEL).upper(),
        mail_user=opts.get("-m"),
    )
    if not args.pattern or not args.bucket:
        usage_error("--pattern and --bucket are required")
    if args.compress_mode not in COMPRESS_MODES:
        usage_error(f"--compress must be one of {COMPRESS_MODES}")
    if args.log_level not in LOG_LEVELS:
        usage_error(f"--log-level must be one of {LOG_LEVELS}")
    return args


def acquire_lock(lock_path):
    """Non-blocking exclusive flock on `lock_path`; None if another run holds it."""
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except BlockingIOError:
        fh.close()
        return None


# --- metadata: append-only JSON lines - per-file archive records, plus
# {"event": "deleted", "tar_name": ...} once a tar is gone from Freezer ---

def read_records(metadata_path):
    if not metadata_path.exists():
        return []
    records = []
    for line in metadata_path.read_text().splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("skipping unparseable metadata line: %r", line)
    return records


def load_metadata(metadata_path):
    """Latest archive record per file, keyed by (folder, file)."""
    return {(r["folder"], r["file"]): r for r in read_records(metadata_path) if "file" in r}


def load_all_tar_names(metadata_path):
    """Every tar ever recorded, including ones superseded by a re-archive (still in Freezer until deleted)."""
    return {r["tar_name"] for r in read_records(metadata_path) if "file" in r}


def load_deleted_tars(metadata_path):
    return {r["tar_name"] for r in read_records(metadata_path) if r.get("event") == "deleted"}


def append_metadata(metadata_path, records):
    if not records:
        return
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with metadata_path.open("a") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in records)


def record_deleted(metadata_path, tar_name):
    append_metadata(metadata_path, [{"event": "deleted", "tar_name": tar_name, "date": date.today().isoformat()}])


# --- discovery ---

def split_pattern(pattern):
    path = Path(pattern)
    return path.parent, path.name


def needs_touch(base_dir):
    """Only nobackup has an auto-cleaner; touching anywhere else would just churn ctime/atime for nothing."""
    return Path(os.path.realpath(base_dir)).is_relative_to(os.path.realpath(NOBACKUP_ROOT))


def discover_folders(base_dir, pattern):
    """Top-level dirs matching `pattern`. pathlib globs match dotdirs, so STATE_DIR_NAME is excluded explicitly."""
    return sorted(p for p in base_dir.glob(pattern) if p.is_dir() and p.name != STATE_DIR_NAME)


def unarchived_files(folder, archived):
    for path in folder.rglob("*"):
        if path.is_file() and (folder.name, str(path.relative_to(folder))) not in archived:
            yield path


def detect_drift(folder, archived):
    """Archived files whose mtime has moved past the one recorded at archive time."""
    drifted = []
    for (folder_name, rel), record in archived.items():
        path = folder / rel
        try:
            if folder_name == folder.name and path.stat().st_mtime > record["mtime"]:
                drifted.append(path)
        except OSError:
            pass
    return drifted


# --- Freezer ---

def run_s3cmd(args, **kwargs):
    """subprocess.run(check=True), but "s3cmd not configured" (exit 78) is logged and returns None."""
    try:
        return subprocess.run(args, check=True, **kwargs)
    except subprocess.CalledProcessError as e:
        if e.returncode != 78:
            raise
        log.error("s3cmd is not configured.  Run `s3cmd --configure` first")
        return None


def bucket_uri(bucket, key=""):
    """s3://<bucket>/<key>, whether or not --bucket was typed with its own "s3://"."""
    if bucket.lower().startswith("s3://"):
        bucket = bucket[len("s3://"):]
    return f"s3://{bucket}/{key}"


def list_bucket_objects(bucket):
    """
    {object name: LastModified date} - the archive date retention goes by. None
    if the listing failed, so callers can't mistake that for "everything's gone".
    """
    proc = run_s3cmd(["s3cmd", "ls", "-l", "-H", bucket_uri(bucket)], capture_output=True, text=True)
    if proc is None:
        return None
    objects = {}
    for parts in (line.split() for line in proc.stdout.splitlines()):
        try:
            objects[parts[-1].rsplit("/", 1)[-1]] = date.fromisoformat(parts[0])
        except (IndexError, ValueError):
            pass
    return objects


def fetch_checksum(bucket, tar_name):
    """Whole-file MD5 from `s3cmd info` (via --preserve's stored attrs - the raw ETag is wrong for multipart)."""
    proc = run_s3cmd(["s3cmd", "info", bucket_uri(bucket, tar_name)], capture_output=True, text=True)
    lines = proc.stdout.splitlines() if proc else []
    return next((l.split(":", 1)[1].strip() for l in lines if l.strip().startswith("MD5 sum:")), None)


# --- archiving ---

def is_probably_compressed(path):
    with path.open("rb") as fh:
        header = fh.read(8)
    return any(header.startswith(sig) for sig in MAGIC_BYTES)


def should_compress(files, mode):
    if mode != "auto":
        return mode == "always"
    total = compressed = 0
    for path in files:
        try:
            size, already = path.stat().st_size, is_probably_compressed(path)
        except OSError:
            continue  # vanished - tarchive() skips it too
        total += size
        compressed += size * already
    return total > 0 and compressed / total < COMPRESSED_FRACTION_THRESHOLD


def next_tar_name(folder, archived, ext):
    """
    First unused "<folder>-<date>[-N]<ext>". Reusing a name would overwrite an
    earlier same-day tar in Freezer while metadata still claims it holds files.
    """
    taken = {r["tar_name"] for r in archived.values()}
    base = f"{folder.name}-{date.today():%Y%m%d}"
    if f"{base}{ext}" not in taken:
        return f"{base}{ext}"
    return next(name for n in count(2) if (name := f"{base}-{n}{ext}") not in taken)


def tarchive(folder, new_files, bucket, archived=None, compress_mode="auto"):
    """Tar `new_files`, upload to Freezer, return one metadata record per file that made it in."""
    if not new_files:
        return []
    compress = should_compress(new_files, compress_mode)
    ext = ".tar.gz" if compress else ".tar"
    tar_name = next_tar_name(folder, archived or {}, ext)

    added = []  # (path, stat)
    with tempfile.NamedTemporaryFile(suffix=ext) as tmp:
        with tarfile.open(tmp.name, "w:gz" if compress else "w") as tar:
            for path in new_files:
                # nobackup is live: skip a vanished file (next run retries it) rather than abort the tar.
                # INFO - expected churn, not worth an alert.
                try:
                    st = path.stat()
                    tar.add(path, arcname=str(Path(folder.name) / path.relative_to(folder)))
                except OSError as e:
                    log.info("skipping %s - vanished before it could be archived: %s", path, e)
                    continue
                added.append((path, st))
        # --preserve forced rather than left to ~/.s3cfg: fetch_checksum() relies on it
        if not added or run_s3cmd(["s3cmd", "put", "--preserve", tmp.name, bucket_uri(bucket, tar_name)]) is None:
            return []
        checksum = fetch_checksum(bucket, tar_name)

    log.info("archived %d file(s) into %s", len(added), tar_name)
    return [
        {"folder": folder.name, "file": str(path.relative_to(folder)), "tar_name": tar_name,
         "size": st.st_size, "mtime": st.st_mtime, "checksum": checksum}
        for path, st in added
    ]


def touch_pending(folder, archived, extra_paths=(), dry_run=False, now=None):
    """
    Refresh atime on pending files (unarchived, plus `extra_paths` - drifted
    files left as-is) whose newer of atime/ctime is TOUCH_THRESHOLD_DAYS old.

    `touch -a`, not os.utime(): keeps the real mtime (tars record it, drift
    detection relies on it) while still bumping ctime, and needs only write
    access - explicit times need ownership, ruling out collaborators' files.
    `now` is for tests (ctime can't be backdated).
    """
    now = time.time() if now is None else now
    stale = []
    for path in [*unarchived_files(folder, archived), *extra_paths]:
        try:
            st = path.stat()
        except OSError:
            log.info("skipping %s - vanished before it could be touched", path)
            continue
        if now - max(st.st_atime, st.st_ctime) >= TOUCH_THRESHOLD_DAYS * 86400:
            stale.append(path)

    if dry_run:
        log.info("[dry-run] would touch %d pending file(s) in %s", len(stale), folder.name)
        return
    failures = []
    for i in range(0, len(stale), TOUCH_BATCH_SIZE):
        # -c: don't recreate a file that vanished since the stat() above
        proc = subprocess.run(["touch", "-a", "-c", "--", *stale[i:i + TOUCH_BATCH_SIZE]],
                              capture_output=True, text=True, errors="replace")
        if proc.returncode:
            failures += [line.strip() for line in proc.stderr.splitlines() if line.strip()]
    if failures:  # WARNING: these really will be deleted if nobody acts
        log.warning("could not touch %d pending file(s) in %s - nobackup's auto-cleaner may delete them "
                    "before they're archived: %s", len(failures), folder.name, "; ".join(failures))
    log.info("touched %d pending file(s) in %s", len(stale) - len(failures), folder.name)


# --- destructive steps: only warned about unless their flag is passed ---

def manual_command(args, flag):
    """This run's invocation plus `flag`, for warnings to quote. Keeps non-default settings so a hand run matches."""
    argv = ["runner_archive", "--pattern", args.pattern, "--bucket", args.bucket]
    if args.compress_mode != "auto":
        argv += ["--compress", args.compress_mode]
    if args.retention_days != DEFAULT_RETENTION_DAYS:
        argv += ["--retention-days", str(args.retention_days)]
    return shlex.join(argv + [flag])


def delete_expired_archives(bucket, metadata_path, retention_days=DEFAULT_RETENTION_DAYS,
                            delete_expired=False, dry_run=False, delete_command=None):
    """Once-per-run pass over the pattern-wide metadata. `delete_command` is quoted in the warning."""
    if retention_days <= 0:
        return
    tar_names = load_all_tar_names(metadata_path) - load_deleted_tars(metadata_path)
    if not tar_names:
        return
    objects = list_bucket_objects(bucket)
    if objects is None:
        return

    expired = []  # (tar_name, age_days)
    for tar_name in sorted(tar_names):
        if tar_name not in objects:
            # deleted by hand: just record it - bookkeeping, not a deletion, so no flag needed
            if dry_run:
                log.info("[dry-run] %s already absent from Freezer - would record as deleted", tar_name)
            else:
                log.info("%s already absent from Freezer - recording as deleted", tar_name)
                record_deleted(metadata_path, tar_name)
        elif (age := (date.today() - objects[tar_name]).days) >= retention_days:  # mirrored in archive_tool
            expired.append((tar_name, age))
    if not expired:
        return

    if not delete_expired:
        # One summary WARNING per run: expired tars stay expired every run, so per-tar warnings would be noise.
        for tar_name, age in expired:
            log.info("%s is past the %d-day retention period (%d days old)", tar_name, retention_days, age)
        oldest, oldest_age = max(expired, key=lambda e: e[1])
        log.warning("%d archive(s) past the %d-day retention period (oldest: %s, %d days old) - Freezer copies "
                    "left as-is. To delete them, run: %s (add --dry-run first to preview)",
                    len(expired), retention_days, oldest, oldest_age,
                    delete_command or "runner_archive with --delete-expired")
        return

    for tar_name, age in expired:
        if dry_run:
            log.info("[dry-run] would delete expired archive %s from Freezer (%d days old)", tar_name, age)
        elif run_s3cmd(["s3cmd", "del", bucket_uri(bucket, tar_name)]) is None:
            return  # not configured (logged) - don't record a deletion that didn't happen
        else:
            log.info("deleted expired archive %s from Freezer (%d days old, past %d-day retention)",
                     tar_name, age, retention_days)
            record_deleted(metadata_path, tar_name)


def superseded_tars(folder, archived, drifted):
    """
    For --overwrite: ({old tar: every key it holds}, untouched sibling paths to
    re-bundle) so each drifted file's old tar can be deleted outright. A tar
    with a sibling gone from disk is left for retention - deleting it would
    destroy that file's only copy.
    """
    drifted_keys = {(folder.name, str(p.relative_to(folder))) for p in drifted}
    stale, siblings = {}, []
    for tar_name in {archived[k]["tar_name"] for k in drifted_keys}:
        keys = {k for k, r in archived.items() if r["tar_name"] == tar_name}
        others = sorted(keys - drifted_keys)
        missing = [k for k in others if not (folder / k[1]).exists()]
        if missing:
            log.warning("%s has other file(s) no longer on disk (%s) - leaving it for retention to age out "
                        "instead of deleting it now", tar_name, ", ".join(map(str, missing)))
            continue
        stale[tar_name] = keys
        siblings += [folder / k[1] for k in others]
    return stale, siblings


def process_folder(folder, bucket, metadata_path, compress_mode="auto", dry_run=False,
                   overwrite=False, touch=True, overwrite_command=None):
    """`overwrite_command` is quoted in the drift warning."""
    archived = load_metadata(metadata_path)
    new_files = list(unarchived_files(folder, archived))
    drifted = detect_drift(folder, archived)
    stale, left_as_is = {}, []
    if drifted and overwrite:
        for path in drifted:
            log.info("%s changed since it was archived - re-archiving (--overwrite)", path)
        stale, siblings = superseded_tars(folder, archived, drifted)
        new_files += drifted + siblings
    elif drifted:
        # One summary WARNING per folder, like the expired-tar one.
        for path in drifted:
            log.info("%s changed since it was archived - Freezer copy left as-is", path)
        log.warning("%d file(s) in %s changed since they were archived - Freezer copies left as-is. "
                    "To re-archive them, run: %s (add --dry-run first to preview)",
                    len(drifted), folder.name, overwrite_command or "runner_archive with --overwrite")
        left_as_is = drifted

    if not new_files:
        log.info("no new files in %s", folder.name)
    elif dry_run:
        log.info("[dry-run] would tar %d new file(s) in %s; %d would be recorded as archived "
                 "(no tar written, no metadata recorded)", len(new_files), folder.name, len(new_files))
        for tar_name in stale:
            log.info("[dry-run] would delete superseded archive %s from Freezer", tar_name)
    else:
        records = tarchive(folder, new_files, bucket, archived=archived, compress_mode=compress_mode)
        append_metadata(metadata_path, records)
        produced = {(r["folder"], r["file"]) for r in records}
        for tar_name, keys in stale.items():
            if not keys <= produced:
                log.warning("%s not fully superseded this run (some file vanished before archiving) - "
                            "leaving it for retention to age out instead", tar_name)
            elif run_s3cmd(["s3cmd", "del", bucket_uri(bucket, tar_name)]) is None:
                break  # not configured (logged) - don't record a deletion that didn't happen
            else:
                log.info("deleted superseded archive %s from Freezer", tar_name)
                record_deleted(metadata_path, tar_name)
        archived = load_metadata(metadata_path)

    if touch:
        touch_pending(folder, archived, extra_paths=left_as_is, dry_run=dry_run)


def main(argv):
    args = parse_args(argv)
    base_dir, glob_expr = split_pattern(args.pattern)
    state_dir = base_dir / STATE_DIR_NAME
    state_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(state_dir / "archive.log", args.log_level)

    lock = acquire_lock(state_dir / "lock")
    if lock is None:
        log.info("lock already held: %s", state_dir / "lock")
        return 1
    metadata_path = state_dir / "metadata.jsonl"
    try:
        touch = needs_touch(base_dir)
        folders = discover_folders(base_dir, glob_expr)
        if not folders:
            log.info("no folders matched pattern %r", args.pattern)
        for folder in folders:
            process_folder(folder, args.bucket, metadata_path, args.compress_mode, dry_run=args.dry_run,
                           overwrite=args.overwrite, touch=touch,
                           overwrite_command=manual_command(args, "--overwrite"))
        delete_expired_archives(args.bucket, metadata_path, retention_days=args.retention_days,
                                delete_expired=args.delete_expired, dry_run=args.dry_run,
                                delete_command=manual_command(args, "--delete-expired"))
    except Exception:
        log.error("unexpected error during archive run", exc_info=True)  # into archive.log, not a bare traceback
        return 1
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
