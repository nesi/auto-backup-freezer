# Implementation Plan


## Phase 0 - Unresolved

- [ ] Decide copy mechanism S3 API vs Globus
- [ ] Decide metadata format
- [ ] Agree `_final` convention with researchers
- [ ] Confirm `775` perms on target `nobackup` dir

## Phase 1 - Set up test data

- [ ] Test data set, in `reannz00001`
- [ ] Kept backed up separately, since test runs will deliberately interrupt/kill copies against it
- [ ] Repeatable reset workflow, so Phase 5 tests start from a known clean state each time
- [ ] Test round trip status of data.

## Phase 2 - tool architecture

- [ ] system python3, stdlib only
- [ ] Log per run, written somewhere. `logging`  Usual output and logging options (debug, info, warning, error)
- [ ] CLI stuff `getopt`
- [ ] set up some tests `unittest`
- [ ] other libs
    - `json` - metadata, 
    - `hashlib` sha256, checksums `tarfile` - tar + hash-while-streaming in one pass, instead of shelling out to `tar` and `sha256sum` separately
    - `subprocess` - `s3cmd`, `scrontab`
    - `pathlib` - folder discovery
    - `fcntl` - lock file

## Phase 3 - tool 1 discovery/diff

- [ ] Pattern matching rules.
- [ ] Enumerate `_final` folders
- [ ] List Freezer contents (`s3cmd ls -l -H`)
- [ ] Cross-reference metadata list
- [ ] Diff new-files list

## Phase 4 - Tool 1 archive

- [ ] Tar step (compression)
- [ ] Write to Freezer
- [ ] Record new archive in metadata list
- [ ] `touch` files not yet archived
- [ ] Resumability  safe retry on interrupted write, no duplicate tars
- [ ] Concurrency guard, lock file (on top of `scrontab`'s own serialization)

## Phase 5 - tool2 setup CLI

- [ ] Collect params (folder, bucket, schedule)
- [ ] Validate (folder writable, Freezer access, perms)
- [ ] Install/update `scrontab` entry (marker-tagged, idempotent)
- [ ] Print summary

## Phase 6 - tool validation

- [ ] Idempotency: run twice, no duplicate tars/entries
- [ ] Resumability: kill mid-run, confirm clean retry
- [ ] Confirm `touch` resets mtime as the auto-cleaner expects
- [ ] Tool 2 re-run updates (not duplicates) the `scrontab` entry

## Phase 7 - Rollout

- [ ] Interanal testing
- [ ] get code reviewed. (by a person ideally)
- [ ] Show users tool operation.

## Next Steps

- interupt handling
- Multiple researchers on one project (locking beyond single-install)
- Cross-project tape-head contention (Slurm reservation / semaphore / jitter)
- Centralised logging (Loki/Alloy)
- 2-year Freezer deletion
- Generalised tool for all researchers (`opt-nesi-bin`)
- document
