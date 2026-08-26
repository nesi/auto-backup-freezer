# Implementation Plan: Tool 1 + Tool 2

## Phase 0 — Prerequisites

- [ ] Decide copy mechanism S3 API vs Globus
- [ ] Decide metadata format
- [ ] Agree `_final` convention with researchers
- [ ] Confirm `775` perms on target `nobackup` dir

## Phase 1 — Test data (reannz00001)

- [ ] Test data set, in `reannz00001`
- [ ] Kept backed up separately, since test runs will deliberately interrupt/kill copies against it
- [ ] Repeatable reset workflow, so Phase 5 tests start from a known clean state each time

## Phase 2 — Tool 1: discovery/diff

- [ ] Enumerate `_final` folders
- [ ] List Freezer contents (`s3cmd ls -l -H`)
- [ ] Cross-reference metadata list
- [ ] Diff new-files list

## Phase 3 — Tool 1: archive

- [ ] Tar step (compression)
- [ ] Write to Freezer
- [ ] Record new archive in metadata list
- [ ] `touch` files not yet archived
- [ ] Resumability: safe retry on interrupted write, no duplicate tars
- [ ] Concurrency guard: lock file (on top of `scrontab`'s own serialization)

## Phase 4 — Local logging

- [ ] Log per run, written somewhere. Usual output and loggin options (debug, info, warning, error)

## Phase 5 — Tool 2: setup CLI

- [ ] Collect params (folder, bucket, schedule)
- [ ] Validate (folder writable, Freezer access, perms)
- [ ] Install/update `scrontab` entry (marker-tagged, idempotent)
- [ ] Print summary

## Phase 6 — Validation

- [ ] Idempotency: run twice, no duplicate tars/entries
- [ ] Resumability: kill mid-run, confirm clean retry
- [ ] Confirm `touch` resets mtime as the auto-cleaner expects
- [ ] Tool 2 re-run updates (not duplicates) the `scrontab` entry

## Phase 7 — Rollout

- [ ] Show users tool operation.


## Next Steps

- interupt handling
- Multiple researchers on one project (locking beyond single-install)
- Cross-project tape-head contention (Slurm reservation / semaphore / jitter)
- Centralised logging (Loki/Alloy)
- 2-year Freezer deletion
- Generalised tool for all researchers (`opt-nesi-bin`)
- document
