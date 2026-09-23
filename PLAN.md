# Implementation Plan

## Phase 0,Unresolved

- [x] Decide copy mechanism S3 API vs Globus
- [x] Decide metadata format
- [x] Agree `_final` convention with researchers
- [x] Confirm `775` perms on target `nobackup` dir
- [ ] names (`archive-tool` and `runner-archive` (runner first to avoid tab complete confusion))

## Phase 1 Set up test data

- [x] Test data set, in `reannz00001`
- [x] Kept backed up separately, since test runs will deliberately interrupt/kill copies against it
- [x] Repeatable reset workflow, so Phase 5 tests start from a known clean state each time
- [x] Test round trip status of data.

## Phase 2 tool architecture

- [x] system python3, stdlib only
- [x] Log per run, written somewhere. `logging`  Usual output and logging options (debug, info, warning, error)
- [x] CLI stuff `getopt`
- [x] set up some tests `unittest`
- [x] other libs
   ,`json`,metadata
   ,`tarfile`,real tar creation (see Phase 4)
   ,`subprocess`,`s3cmd`, `scrontab`
   ,`pathlib`,folder discovery
   ,`fcntl`,lock file
   ,`hashlib` unneeded,checksum read back from `s3cmd info` (via `--preserve`) instead.

## Phase 3 tool 1 discovery/diff

- [x] Pattern matching rules.
- [x] Enumerate `_final` folders
- [x] List Freezer contents (`s3cmd ls -l -H`),`list_freezer_contents()`, not yet wired into the diff
- [x] Cross-reference metadata list
- [x] Diff new-files list
- [ ] Decide whether `diff_new_files()` should also cross-check the Freezer listing, or metadata alone is enough for now

## Phase 4 Tool 1 archive

- [x] Tar step (compression),`tarchive()`, `should_compress()` decides gzip
- [x] Write to Freezer,`s3cmd put --preserve`, verified against real Freezer (`test-guest-collection-11926`)
- [x] Record new archive in metadata list,`append_metadata()`, one JSON line per file incl. checksum
- [x] `touch` files not yet archived `touch_pending()`
- [x] Touch only when the base dir's real path is under `/nesi/nobackup` (`needs_touch()`), so watched folders can live in `project`/`home` without their mtimes being rewritten.
- [x] Resumability, safe retry, no duplicate tars,`next_tar_name()` fixed a bug: two same-day runs computed the same tar name, so the second silently overwrote the first while metadata still claimed both were archived. Verified live. Mid-upload crashes are safe by construction (nothing recorded until upload+checksum succeed).
- [ ] Open, lower severity: no cleanup of an abandoned incomplete multipart upload left by a killed run,storage/cost leak, not a correctness bug
- [x] Concurrency guard, lock file (on top of `scrontab`'s own serialization),`acquire_lock()`
- [x] Drift detection + `--overwrite`,`detect_drift()` compares recorded vs. current mtime, warns by default; `--overwrite` re-archives drifted files under a new tar name
- [x] Drift warning summarised, one WARNING per folder (per-file detail at INFO) instead of one per file per run

## Phase 4b,Tool 1 retention

- [x] `list_bucket_objects()`, bucket listing with each object's `LastModified` as archive date; returns `None` (not `{}`) on failure so that isn't mistaken for "delete everything"
- [x] `delete_expired_archives()`: once-per-run pass matching metadata `tar_name`s against the bucket listing, finds tars past `archive_date + retention_days`
- [x] Warn-only by default, same as drift: expired tars get one summary WARNING per run (count, oldest, how to delete), per-tar detail at INFO. Scheduled runs never delete from Freezer
- [x] `--delete-expired` (`-d`) flag, the only way expired tars get `s3cmd del`'d. Never written into `scrontab`
- [x] Tar missing from the bucket listing (deleted by hand) recorded as `deleted` immediately, no `s3cmd del` call
- [x] Otherwise `s3cmd del`, then append a `deleted` event record (append-only; `load_metadata()` skips these, `load_deleted_tars()` reads them back)
- [x] `deleted` event only recorded if `s3cmd del` actually ran, fixed a bug: s3cmd-not-configured (exit 78) returned `None` and a deletion was recorded anyway. Same fix on the `--overwrite` superseded-tar path. Unit-tested both
- [x] `--dry-run` covers this pass
- [x] `--retention-days 0` disables retention (no warnings, nothing expires) for that entry
- [x] Verified live: "deleted by hand" path, re-run no-op all confirmed with real `s3cmd` output
- [ ] Verify live: warn-only summary and `--delete-expired` deletion against real `s3cmd`
- [x] Wired into `archive_tool` (Phase 5)

## Phase 4c,Tool 1 nobackup auto-cleaner protection

- [x] Age = newer of atime and ctime (what the cleaner checks), not mtime. mtime ≤ ctime always, so today's check never misses a file, it just touches some needlessly
- [x] Threshold 60 days, so pending files never reach the ~76-day list or trigger the warning email. Leaves ~16 days' margin for a fortnightly cycle plus a missed run.
- [x] Touch atime only, keeping the real mtime: `touch -a -c -- <files>`, in batches. Any timestamp change also bumps ctime, so both of the cleaner's clocks reset
  - Not `os.utime(path, (now, mtime))`: setting explicit times needs file *ownership*, whereas "atime to now, leave mtime" needs only write access, so collaborators' group-writable files couldn't be touched any more. `os.utime` can't express "leave mtime"; coreutils `touch -a` can (`UTIME_OMIT`)
  - Fixes: tars no longer record a fake mtime for files that sat pending past the threshold, and touching no longer bumps mtime on drifted files left as-is
- [x] Per-file failures don't abort the run (MANUAL_TESTS items):
  - file vanished between discovery and touch (`stat()` is unguarded today) → skip, INFO
  - no permission (e.g. a collaborator's `600` file) → one WARNING naming the files (from `touch`'s stderr), since the cleaner will delete them; rest of the run continues
- [x] Unit tests:
  - old mtime but recent atime → not touched; old atime and ctime → touched
  - 60-day boundary (59 not touched, 60 touched)
  - mtime unchanged after touching (real `touch` on a temp file)
  - vanished file skipped; permission failure warns and the run continues
  - batching, and no `touch` call at all when nothing is stale
  - `--dry-run` still only reports "would touch N"
- [ ] Verify live on a collaborator's group-writable file

## Phase 5 tool2 setup CLI

- [x] Collect params (folder, bucket, schedule, `--retention-days`, optional with SPEC defaults)
- [x] Validate: folder exists/writable, Freezer access check (`s3cmd ls` on the bucket) and `775`/group perms warning, all inline in `validate()`
- [x] Install/update `scrontab` entry (marker-tagged, idempotent),now embeds retention fields too, `ENTRY_RE` updated to match
- [x] Print summary
- [x] `status` command: archived count, last run, next run, retention-days, expired/deleted counts, plus the exact `runner_archive … --delete-expired` command when anything's expired (`retention_status()`, own `list_bucket_dates()`,deliberately duplicated from `runner_archive.py`, not imported, per one-file-per-command convention). Verified live.
- [x] `add` writes a `#SCRON --job-name=archive_tool-<id>` directive above each entry; add/remove/remove --all handle it with its cron line. Older entries without it still parse
- [x] `status` "next run" from `squeue --me` (pending scron job's start time, fixed `SLURM_TIME_FORMAT`), one call for all entries; "running now", "not scheduled" (e.g. Slurm-disabled), and "unknown" (squeue unavailable) cases. `touch after` line removed. Verified live against real `squeue`
- [x] Group-writable warning in `validate()` only for shared space (`/nesi/project`, `/nesi/nobackup`), not e.g. `/home`
- [ ] Default schedule `0 2 * * *` actually runs at 14:00 NZST (controller is UTC). Pick a default that's intended in local time, or document it
- [x] `--dry-run` on `add`,`run_archive(dry_run=True)` shells out to `runner_archive --dry-run` for the file-level preview plus a would-add/would-update line for the scrontab entry; writes nothing
- [x] `validate()` Freezer-access check (distinguishes s3cmd missing / not configured / no bucket access) and 775/group-perms warning (stderr only, never fails validation)
- [ ] Verify live: copy-paste the quoted `--delete-expired` command from `status` (with and without `--dry-run`)

## Phase 6 tool validation

- [x] Idempotency: run twice, no duplicate tars/entries,unit-tested and confirmed live
- [x] Resumability: kill mid-run, confirm clean retry,see Phase 4's `next_tar_name()` fix. Not yet tested: an actual kill signal mid-`s3cmd put` (only the same-day-duplicate-run case was verified live); the abandoned-multipart-upload leak from Phase 4 is still unaddressed
- [x] Edge-case pass found and fixed three real bugs (live-verified where applicable):
   ,a file vanishing mid-run (nobackup is live) used to crash `tarchive()` and abort the whole run,now skipped with a warning
   ,a pattern containing a single quote (e.g. `O'Brien_final`) broke scrontab-line quoting,now rejected by `validate()`
   ,`cmd_add()` let a `validate()` failure crash with a raw traceback,now caught and reported as a clean CLI error
- [x] Retention boundary conditions (retention-day cutoff, one day short),all correct, no off-by-one
- [x] Tool 2 re-run updates (not duplicates) the `scrontab` entry,`add_or_update_entry()`, marker-based replace, unit-tested

## Phase 7 Rollout

- [ ] Interanal testing
- [ ] get code reviewed. (by a person ideally)
- [ ] Show users tool operation.

## Next Steps

- interupt handling
- Multiple researchers on one project (locking beyond single-install)
- Cross-project tape-head contention (Slurm reservation / semaphore / jitter)
- Centralised logging (Loki/Alloy)
- Generalised tool for all researchers (`opt-nesi-bin`)
- document
