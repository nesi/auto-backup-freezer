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
- [x] Resumability, safe retry, no duplicate tars,`next_tar_name()` fixed a bug: two same-day runs computed the same tar name, so the second silently overwrote the first while metadata still claimed both were archived. Verified live. Mid-upload crashes are safe by construction (nothing recorded until upload+checksum succeed).
- [ ] Open, lower severity: no cleanup of an abandoned incomplete multipart upload left by a killed run,storage/cost leak, not a correctness bug
- [x] Concurrency guard, lock file (on top of `scrontab`'s own serialization),`acquire_lock()`
- [x] Drift detection + `--overwrite`,`detect_drift()` compares recorded vs. current mtime, warns by default; `--overwrite` re-archives drifted files under a new tar name

## Phase 4b,Tool 1 retention deletion

- [x] `list_bucket_objects()`, bucket listing with each object's `LastModified` as archive date; returns `None` (not `{}`) on failure so that isn't mistaken for "delete everything"
- [x] `--retention-days` (default 730) / `--retention-warn-days` (default 30) flags, `-r` short form for the former
- [x] `delete_expired_archives()`: once-per-run pass matching metadata `tar_name`s against the bucket listing, finds tars past `archive_date + retention_days`
- [x] Warn-before-delete: logs once a tar enters the warn window
- [x] Tar missing from the bucket listing (deleted by hand) recorded as `deleted` immediately, no `s3cmd del` call
- [x] Otherwise `s3cmd del`, then append a `deleted` event record (append-only; `load_metadata()` skips these, `load_deleted_tars()` reads them back)
- [x] `--dry-run` covers this pass
- [x] `--retention-days 0` disables deletion for that entry
- [x] Verified live: warn path, "deleted by hand" path, re-run no-op all confirmed with real `s3cmd` output
- [x] Wired into `archive_tool` (Phase 5)

## Phase 5 tool2 setup CLI

- [x] Collect params (folder, bucket, schedule, `--retention-days`/`--retention-warn-days`, optional with SPEC defaults)
- [x] Validate: folder exists/writable,Freezer access check and `775`/group perms warning were TODOs in `validate()` (closed below)
- [x] Install/update `scrontab` entry (marker-tagged, idempotent),now embeds retention fields too, `ENTRY_RE` updated to match
- [x] Print summary
- [x] `status` command: archived count, last run, retention-days, due/pending/deleted counts (`retention_status()`, own `list_bucket_dates()`,deliberately duplicated from `runner_archive.py`, not imported, per one-file-per-command convention). Verified live.
- [x] `--dry-run` on `add`,`preview_archive_run()` shells out to `runner_archive --dry-run` for the file-level preview plus a would-add/would-update line for the scrontab entry; writes nothing
- [x] `enable`/`disable` subcommands,`disable_entry()`/`enable_entry()` comment/uncomment the managed line in place
- [x] `validate()` Freezer-access check (`check_freezer_access()`) and 775/group-perms warning (`warn_on_bad_group_permissions()`, never fails validation)

## Phase 6 tool validation

- [x] Idempotency: run twice, no duplicate tars/entries,unit-tested and confirmed live
- [x] Resumability: kill mid-run, confirm clean retry,see Phase 4's `next_tar_name()` fix. Not yet tested: an actual kill signal mid-`s3cmd put` (only the same-day-duplicate-run case was verified live); the abandoned-multipart-upload leak from Phase 4 is still unaddressed
- [x] Edge-case pass found and fixed three real bugs (live-verified where applicable):
   ,a file vanishing mid-run (nobackup is live) used to crash `tarchive()` and abort the whole run,now skipped with a warning
   ,a pattern containing a single quote (e.g. `O'Brien_final`) broke scrontab-line quoting,now rejected by `validate()`
   ,`cmd_add()` let a `validate()` failure crash with a raw traceback,now caught and reported as a clean CLI error
- [x] Retention boundary conditions (warn-window edge, retention-day cutoff, one day short),all correct, no off-by-one
- [x] Confirm `touch` resets mtime as the auto-cleaner expects,`os.utime(path, None)`, unit-tested. Can't confirm against the real 90-day cleaner without a wait or NeSI sysadmin input
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
