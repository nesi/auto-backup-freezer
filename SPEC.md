# Technical Specification: User-Space Archiving from `nobackup` to Freezer


## 1. Background

The Auckland Genomics project (`uoa03387`) currently backs up data manuallynresearchers separate data into "share" and "not-share" folders, compress/tar it, and copy it to Freezer. This is error-prone and labor-intensive, and researchers must remember to act before data on `nobackup` is auto-deleted.

This spec covers a pair of user-run tools that do the same job package finished data and copy it to Freezer without a researcher needing to remember or do it by hand each time.

- Most source files are already compressed, so archiving only needs to **tar** (maybe test this tho)
- Freezer data is rarely recalled **2 years is a sufficient retention period**.
- one tape copy is acceptable
- `nobackup` auto-deletes files after **90 days** of inactivity. things should not be deleted if being worked on by tool.
- Users don't have access to regular cron on this compute. Scheduling instead uses **[scrontab](https://slurm.schedmd.com/scrontab.html)**

## Concepts

| Term | Meaning |
|---|---|
| **`nobackup`** | Project scratch filesystem (on Weka)  and a 90-day inactivity auto-cleaner. |
| **Freezer** | Tape backed archival storage, accessed as an S3-compatible bucket (`s3cmd`) and via Globus. |
| **Folder Pattern** | A pattern matching system (in the uoa03387 case `*_final`) - the researcher's signal that a folder's contents are ready to archive. |
| **Metadata list** | A record kept on `nobackup` of which files have been archived and into which tar. Files not yet in it are `touch`ed so the auto-cleaner doesn't reap them. Exact format not yet settled - see Open Questions (§5.2). |
| **`scrontab`** | Slurm's cron equivalent (cron not available to users). Each entry runs as a Slurm job. |
| **Freezer head** | A physical tape-drive slot, the number of archiving operations Freezer can service in parallel across *all* users. |

## Tools

Two tools, both run under the researcher's own account:

- **Tool 1 (archiver):** does the actual work - scans `_final` folders, tars new/changed data, writes it to Freezer, updates the metadata list. Runs unattended, as a `scrontab` entry, 
- **Tool 2 (setup):** a small user-facing CLI. Takes the parameters a researcher needs to set (which folder to watch, which Freezer bucket, schedule) and installs/updates the `scrontab` entry that invokes Tool 1 with those parameters.

Splitting these keeps Tool 1 simple and non-interactive while Tool 2 is the only part a researcher has to think about.

### Tool 1 - Archiver Script

Runs on whatever schedule Tool 2 installed:

1. **Concurrency guard** - `scrontab` already won't submit an entry's next scheduled occurrence until the previous one has finished, so a Tool 1 run can't overlap its own next scheduled run. What that doesn't cover: a researcher manually invoking Tool 1 by hand while the scheduled job is also running, or two independently-scheduled installs on the same project.
3. **List what's already on Freezer** for this folder's path (`s3cmd ls -l -H`).
4. **Cross-reference the metadata list** for files already recorded as archived.
5. **Diff** against the folder's current contents to build a "new files" list - this is also what avoids re-archiving duplicates. If empty, move to the next folder.
6. **Compress** Maybe?
7. **Tar and write to Freezer.**  Naming scheme.
8. **Record the new archive in the metadata list** format not set.
9. `touch` every file not yet archived.

Throughout log every step locally. A failed or interrupted copy must be detected and resumed/retried on the next run rather than leaving a partial tar on Freezer.

### Tool 2 - Setup Tool

A CLI a researcher runs once (and re-runs to change settings):

1. Collect: which folder to watch (defaults to the project's `nobackup`), the Freezer bucket, and a schedule (default: daily).
2. **Validate** before installing anything: confirm the folder exists and is writable, confirm Freezer credentials/access work (e.g. a lightweight `s3cmd ls` against the target bucket), confirm `775`/group permissions look sane and warn if not.
3. **Install or update** a `scrontab` entry invoking Tool 1 with these parameters. The entry is tagged with a marker comment so Tool 2 can find and replace its own prior line on re-run, without touching any other entries in the researcher's `scrontab` table. Editing is always read-modify-write against the existing table (`scrontab -l` / `scrontab -e`), never a blind overwrite.
4. Print a summary
5. **Status command** - a way to check current archive state (what's been archived, what's pending, when the last run happened) without reading the metadata list by hand.
6. **`--dry-run`** - show what a real run would do (files that would be archived, `scrontab` entry that would be installed/changed) without writing anything.
7. If a researcher needs to delete an archive, they do it themselves via the regular `s3cmd`.

## Requirements

- Automatically detect data on `nobackup` marked finished, and archive it to Freezer without further manual steps.
- Never lose data to the 90-day `nobackup` auto-cleaner while it's still pending archival.
- Resumable and safe to re-run: no duplicate or corrupt archives from an interrupted run.
- No credentials, accounts, or permissions beyond what the researcher already has for their own project.
- **Permissions:** shared write access within a project directory needs `775` group permissions so collaborators can write without clobbering each other.
- **Local logging:** Tool 1 writes a plain-text/structured log  on every run the source of truth a researcher or support would tail/read directly if something looks wrong.
- **Retention:** archived data is kept on tape for 2 years; the metadata file records archive dates so automatic deletion after that period could be added later without a data audit.
- **Scheduling:** all unattended runs go through `scrontab`, not regular cron, because regular cron isn't available to users.
- If an already-archived file is later modified, detected drift (recorded state vs. current file) is a log warning at run-time, not a stored metadata entry.
- The Freezer-side copy is otherwise left alone unless the researcher opts in - a `--overwrite` flag should make explicit whether a changed source file gets re-archived or just logged.

### Open questions / risks

- **Multiple researchers, one project.** Current design assumes exactly one person sets up, `scrontab`, config changes must be done by same user (note, pattern can specify across whole project).
  If same config needs to be editable by whole project (questionable), there are options.
  - Used user-systemd, while still has to run as user, those files can be given group permissions.(messy)
  - File based config. Scron would read the config at execute time. (still requires someone to set up their own crontab, messy)
  - Globus compute can maybe do something?
- **No dead-man's-switch.** If Tool 1 stops mid run, what happens?
- **Copy mechanism**: whether the tar-and-write-to-Freezer step uses the S3 API (`s3cmd`/similar) or Globus.
- **What "visibility into what's archived" should actually be**: is the metadata file enough, or does this need a log, an email, or a small "check status" command in Tool 1/Tool 2 rather than expecting researchers to read a manifest directly?
- **Metadata format** - what fields does an entry actually need? At minimum: source path, archive/tar name, archive date, size, checksum, mtime. Consider adopting **[BagIt](https://en.wikipedia.org/wiki/BagIt)** as the on-disk metadata standard rather than a bespoke format.
- How should discrepencies between disk and metadata be treated? I'd suggest flags. Default behavor `--safe` = no delete, no update, `--overwrite` = no delete, allow update, `--sync` allow overwrite and delete. 
- **`nobackup` auto-delete mechanics** - Uses atime. what frequency should tool1 uses to keep pending files alive, touching one period out may be too close maybe touching 3 out for safety.

### Out of scope, for now
- A generic tool accessable to all researchers, put in `opt-nesi-bin`.
- Tool might push increased use of archive, Freezer's actual write throughput is bounded by a small number of physical tape heads, we would want to make sure that this tool does not interfere with regular usage.
  - **Slurm reservation.** Since every Tool 1 run is already a Slurm job by construction (via `scrontab`), this may be closer to configuration than new infrastructure: have Tool 2 set a reservation on the `#SCRON` directive with a hard cap (e.g. 2 running at once), and Slurm's own scheduler enforces the limit across every project using it no bespoke queue to build or own.
  - **Semaphore.** A small, fixed number of lock files in a well-known shared location (one per available Freezer head). Tool 1 tries to `flock` one before writing, if none are free, it waits or defers to the next scheduled run. Simpler to reason about than a Slurm partition, but it's infrastructure someone still has to place and maintain, and doesn't get fairness/scheduling for free the way Slurm does.
  - **Jitter.** Tool 2 assigns each project a randomised offset within the scheduling window, spreading start times out to reduce collision odds.
- Automatic deletion of data from Freezer after the 2-year retention period.
- A `--sync` flag on Tool 1 (mirror deletions from `nobackup` to Freezer).
- Centralised logging (loki/alloy), best handled seperately to tool IMO.