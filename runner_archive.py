#!/bin/python
"""
Tool 1
"""

import fcntl
import getopt
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

PROGNAME = "runner_archive"

DEFAULT_PATTERN = "*_final"

USAGE = f"""usage: {PROGNAME} [OPTIONS]

  -f, --folder PATH     nobackup folder to scan (required)
  -b, --bucket NAME      Freezer bucket to archive into (required)
  -p, --pattern GLOB     top-level folder glob to archive (default: "{DEFAULT_PATTERN}")
  -c, --compress MODE    auto (default), always, or never
  -v, --verbose          debug logging
  -h, --help             show this help
"""

COMPRESS_MODES = ("auto", "always", "never")

# Signatures for formats that are already compressed - recompressing these
# wastes CPU for near-zero size change (sometimes a slight increase).
# BAM is BGZF (block gzip), so it shares gzip's magic bytes.
MAGIC_BYTES = [
    b"\x1f\x8b",              # gzip / BAM (BGZF)
    b"BZh",                   # bzip2
    b"\xfd7zXZ\x00",          # xz
    b"\x28\xb5\x2f\xfd",      # zstd
    b"PK\x03\x04",            # zip
    b"CRAM",                  # CRAM
]
COMPRESSED_FRACTION_THRESHOLD = 0.9  # tunable: fraction of bytes already-compressed to skip compression

log = logging.getLogger(PROGNAME)


class LockHeld(Exception):
    """Another run already holds the lock file."""


def parse_args(argv):
    try:
        opts, _args = getopt.getopt(
            argv, "f:b:p:c:vh",
            ["folder=", "bucket=", "pattern=", "compress=", "verbose", "help"],
        )
    except getopt.GetoptError as e:
        print(f"{PROGNAME}: {e}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(2)

    opts = dict(opts)
    if "-h" in opts or "--help" in opts:
        print(USAGE)
        sys.exit(0)

    folder = opts.get("-f") or opts.get("--folder")
    bucket = opts.get("-b") or opts.get("--bucket")
    pattern = opts.get("-p") or opts.get("--pattern") or DEFAULT_PATTERN
    compress_mode = opts.get("-c") or opts.get("--compress") or "auto"
    verbose = "-v" in opts or "--verbose" in opts

    if not folder or not bucket:
        print(f"{PROGNAME}: --folder and --bucket are required", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(2)

    if compress_mode not in COMPRESS_MODES:
        print(f"{PROGNAME}: --compress must be one of {COMPRESS_MODES}", file=sys.stderr)
        sys.exit(2)

    return Path(folder), bucket, pattern, compress_mode, verbose


def setup_logging(verbose, log_path):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )


def acquire_lock(lock_path):
    """
    Non-blocking flock. Returns an open handle the caller must close when
    done. Raises LockHeld if another process already holds it.
    SPEC "How it works" 3.1 step 1 - NOTE: assumes flock is atomic on
    nobackup's filesystem (Weka); unconfirmed, see SPEC Open Questions.
    """
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise LockHeld(f"lock already held: {lock_path}")
    return fh


# Format not settled - see SPEC Open Questions ("Metadata format isn't
# settled"). For now: metadata is just a list of archived files, one JSON
# record per line (JSONL), append-only. No event types.

def load_metadata(metadata_path):
    """Read the metadata list, return {(folder, file): record}."""
    archived = {}
    if not metadata_path.exists():
        return archived
    with metadata_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A crash mid-append can leave a partial trailing line.
                log.warning("skipping unparseable metadata line: %r", line)
                continue
            archived[(record["folder"], record["file"])] = record
    return archived


def append_metadata(metadata_path, file_records):
    """Append file records as new lines. Existing lines are never rewritten."""
    with metadata_path.open("a") as fh:
        for record in file_records:
            fh.write(json.dumps(record) + "\n")

def discover_folders(nobackup_path, pattern=DEFAULT_PATTERN):
    """
    Top-level folders matching `pattern` (default "*_final"). No recursion
    unless the caller passes a pattern with "**" - SPEC's top-level-only
    assumption is a default, not enforced here.
    """
    return sorted(p for p in nobackup_path.glob(pattern) if p.is_dir())


def list_freezer_contents(bucket, folder_name):
    """
    Tar objects already on Freezer for this folder.
    SPEC "How it works" 3.1 step 3.
    """
    # TODO: not started. Depends on the copy mechanism decision (S3 API vs
    # Globus - SPEC Phase 0/Open Questions), which determines both how to
    # list contents and how to invoke it (e.g. `s3cmd ls -l -H`). Also
    # currently unused by diff_new_files() below, which only checks
    # metadata - whether new files should also be cross-checked against a
    # live Freezer listing hasn't been decided either.
    raise NotImplementedError


def unarchived_files(folder, archived):
    """Files under `folder` with no metadata record yet."""
    for path in folder.rglob("*"):
        if path.is_file() and (folder.name, str(path.relative_to(folder))) not in archived:
            yield path


def diff_new_files(folder, archived):
    """
    New files to archive - this is what avoids re-archiving duplicates.
    SPEC "How it works" 3.1 step 5.
    """
    return list(unarchived_files(folder, archived))

def is_probably_compressed(path):
    """Cheap check: does this file's header match a known-compressed format's magic bytes?"""
    with path.open("rb") as fh:
        header = fh.read(8)
    return any(header.startswith(sig) for sig in MAGIC_BYTES)


def should_compress(new_files, mode):
    """
    Whether to compress the tar for this batch of files.
    "auto": skip compression if COMPRESSED_FRACTION_THRESHOLD of total
    bytes already look compressed (weighted by size, not file count, so
    one large BAM isn't outvoted by many tiny text files).
    """
    if mode == "always":
        return True
    if mode == "never":
        return False

    sizes = [p.stat().st_size for p in new_files]
    total = sum(sizes)
    if total == 0:
        return False
    compressed_bytes = sum(
        size for path, size in zip(new_files, sizes) if is_probably_compressed(path)
    )
    return (compressed_bytes / total) < COMPRESSED_FRACTION_THRESHOLD


def tarchive(folder, new_files, bucket, compress_mode="auto"):
    """
    Tar new_files (hashing each while streaming through tarfile - close to
    free, no separate read pass), write the tar to Freezer, return a list
    of file records (one per archived file) ready for append_metadata().
    SPEC "How it works" 3.1 steps 6-7.
    """
    compress = should_compress(new_files, compress_mode)
    tar_name = f"{folder.name}-{date.today():%Y%m%d}" + (".tar.gz" if compress else ".tar")
    # TODO: build the tar with `tarfile.open(..., mode="w:gz" if compress else "w")`,
    # hashing each member's bytes as they're read (hashlib.sha256, or blake2b for speed).
    # TODO: write the resulting tar to Freezer.
    # TODO: known open gap (SPEC Open Questions): a crash between writing
    # the tar and the append_metadata() call below can produce a real
    # duplicate archive on the next run - reconciliation against the
    # Freezer listing (list_freezer_contents) isn't implemented yet.
    raise NotImplementedError


def touch_pending(folder, archived):
    """
    touch every file not yet archived, so the 90-day auto-cleaner doesn't
    reap it. Deliberately does NOT touch already-archived files: nobackup
    has no explicit delete step anywhere in this design, so the auto-
    cleaner reaping an already-archived file once it goes stale is the
    only thing that ever reclaims that space. Touching everything would
    keep every archived file alive on nobackup forever, growing without
    bound instead of settling into steady state.
    """
    for path in unarchived_files(folder, archived):
        os.utime(path, None)


def safety_net_check(nobackup_path, archived):
    """
    Files not yet in metadata approaching the 90-day threshold. Alerting
    uses Slurm's own --mail-user (see SPEC), not a mail library.
    SPEC "How it works" 3.1 step 8.
    """
    # TODO: stat every file under every _final folder not in `archived`;
    # if close to 90 days, emit an alert.
    raise NotImplementedError


def process_folder(folder, bucket, metadata_path, compress_mode="auto"):
    archived = load_metadata(metadata_path)
    new_files = diff_new_files(folder, archived)
    if not new_files:
        log.info("no new files in %s", folder.name)
    else:
        file_records = tarchive(folder, new_files, bucket, compress_mode)
        append_metadata(metadata_path, file_records)
        archived = load_metadata(metadata_path)
    touch_pending(folder, archived)


def main(argv):
    nobackup_path, bucket, pattern, compress_mode, verbose = parse_args(argv)
    state_dir = nobackup_path / ".freezer"
    metadata_path = state_dir / "metadata.jsonl"
    lock_path = state_dir / "lock"
    log_path = state_dir / "archive.log"
    state_dir.mkdir(parents=True, exist_ok=True)

    setup_logging(verbose, log_path)

    try:
        lock_fh = acquire_lock(lock_path)
    except LockHeld as e:
        log.info(str(e))
        return 0

    try:
        for folder in discover_folders(nobackup_path, pattern):
            process_folder(folder, bucket, metadata_path, compress_mode)
        # TODO: safety_net_check() is not implemented yet (see its own
        # docstring) - not calling it here rather than letting an
        # unconditional NotImplementedError crash every run, including
        # ones with nothing to archive.
    finally:
        lock_fh.close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
