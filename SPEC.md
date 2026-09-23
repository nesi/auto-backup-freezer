# Technical Specification: User-Space Archiving to Freezer


## 1. Background

The Auckland Genomics project (`uoa03387`) currently backs up data manuallynresearchers separate data into "share" and "not-share" folders, compress/tar it, and copy it to Freezer. This is error-prone and labor-intensive, and researchers must remember to act before data on `nobackup` is auto-deleted.

This spec covers a pair of user-run tools that do the same job package finished data and copy it to Freezer without a researcher needing to remember or do it by hand each time.

- Most source files are already compressed, so archiving only needs to **tar** (maybe test this tho)
- Freezer data is rarely recalled **2 years is a sufficient retention period**.
- one tape copy is acceptable
- `nobackup` auto-deletes files after **90 days** of inactivity. things should not be deleted if being worked on by tool. Per the [NeSI docs](https://github.com/nesi/support-docs/blob/main/docs/Storage/Automatic_Cleaning_of_Nobackup.md), a file is deleted once its atime **and** ctime are both >90 days old and it was already on the previous fortnightly candidate list (listed, and its owner emailed, at ~76 days).
- Not limited to `nobackup`: the watched folder can just as well be under `/nesi/project/<project>` or `/home/<user>`. The `nobackup` auto-cleaner is handled internally and never exposed to the user.
- Users don't have access to regular cron on this compute. Scheduling instead uses **[scrontab](https://slurm.schedmd.com/scrontab.html)**

## Concepts

| Term | Meaning |
|---|---|
| **`nobackup`** | Project scratch filesystem (on Weka)  and a 90-day inactivity auto-cleaner. |
| **Freezer** | Tape backed archival storage, accessed as an S3-compatible bucket (`s3cmd`) and via Globus. |
| **Folder Pattern** | A pattern matching system (in the uoa03387 case `*_final`) - the researcher's signal that a folder's contents are ready to archive. |
| **Metadata list** | A record kept in the watched folder's base dir (`.freezer/`) of which files have been archived and into which tar. On `nobackup`, files not yet in it are `touch`ed so the auto-cleaner doesn't reap them. Exact format not yet settled - see Open Questions (§5.2). |
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
7. **Tar and write to Freezer.** Named `<folder-name>-<YYYYMMDD>.tar` (`.tar.gz` when compressed). `s3cmd put` always forces `-p`/`--preserve` explicitly, so the upload embeds a real whole-file MD5 regardless of `s3cmd`'s multipart chunking.
8. **Record the new archive in the metadata list**: one line per file - tar name, size/mtime at archive time, MD5 read back from `s3cmd info` after upload.
9. If the base dir is under `/nesi/nobackup` (real path, so symlinks into it count): refresh every file not yet archived whose newer of atime/ctime is at least 60 days old, so pending files never reach the cleaner's ~76-day list. Nowhere else has an auto-cleaner, so it's skipped elsewhere.
   - Uses `touch -a -c` (atime only, in batches): the real mtime is kept (so tars record it and drift detection isn't fooled), ctime is bumped anyway, and it only needs write access - setting explicit times (`os.utime(path, (now, mtime))`) would need file ownership, ruling out collaborators' group-writable files.
   - A file that vanished before it could be touched is skipped (INFO). A file that can't be touched (e.g. a collaborator's `600` file) gets one WARNING per folder naming it, since the cleaner will delete it; the run carries on.
   - The cleaner's published candidate list (`/search/autocleaner/filelists/current/<project>.gz`) isn't used here: it's readable on login03 only, not from the compute nodes `scrontab` jobs run on. Tool 2's `status` shows it instead (below).

Throughout log every step locally. A failed or interrupted copy must be detected and resumed/retried on the next run rather than leaving a partial tar on Freezer.

#### Destructive actions are never unattended

A scheduled run only ever *adds* to Freezer. Anything that deletes from Freezer is warned about on the scheduled run and needs a separate, manual run with an explicit flag to actually happen:

| Trigger | Scheduled run (default) | Manual run |
|---|---|---|
| Archived file changed on disk (drift) | warning, Freezer copy left as-is | `--overwrite`: re-archive it, delete the superseded tar |
| Tar past its retention period | warning, tar left in Freezer | `--delete-expired`: delete it |

The manual run is always `runner_archive` itself - Tool 2 only sets up and reports, and never writes either flag into a `scrontab` entry. Each warning quotes the exact command to run, built from the run's own settings (pattern, bucket and any non-default `--compress`/`--retention-days`, so a hand run judges expiry and re-archives exactly as the scheduled one would), and suggests `--dry-run` first. Tool 2's `status` shows the same command for expired tars. Warnings are summarised (one line per folder for drift, one line per run for retention) rather than one per file/tar - both conditions persist every run until someone acts, so per-item warnings would just be noise that trains people to ignore them. Per-item detail goes to the log at INFO.

#### Retention (per-pattern, once per run)

Runs once per invocation, after the per-folder loop above (metadata file and bucket are shared across every folder matching the pattern, so this is one pass rather than per-folder):

1. List the whole bucket once (`s3cmd ls -l -H s3://bucket/`) and use each object's `LastModified` as its archive date. Match each `tar_name` in the metadata file against this listing.
2. Skip tars already recorded as deleted. If a tar is missing from the bucket listing, it's already gone (deleted by hand) - append a `deleted` event, no `s3cmd del`. This is bookkeeping, not deletion, so it happens on every run regardless of flags.
3. A tar is expired once it's at least `retention_days` old. Without `--delete-expired`: log one summary warning (count, oldest tar, how to delete them) and leave them alone. With `--delete-expired`: `s3cmd del` each, then append its `deleted` event. A `deleted` event is only appended once the `s3cmd del` actually ran. Per-file archive records are never edited/removed, so `archive_tool status` can always answer "was X archived" and "was it deleted" from the one file.

Controlled by `--retention-days` (default 730) and `--delete-expired`. `--retention-days 0` disables retention (no warnings, nothing ever expires) for that entry. `--dry-run` covers this pass too: logs what would be deleted, writes nothing.

### Tool 2 - Setup Tool

A CLI a researcher runs once (and re-runs to change settings):

1. Collect: which folder to watch (any directory the researcher can write to - `nobackup`, `project` or `home`), the Freezer bucket, a schedule (default: daily), and a retention period (default 2 years / 730 days; 0 disables retention for that entry).
2. **Validate** before installing anything: confirm the folder exists and is writable, confirm Freezer credentials/access work (e.g. a lightweight `s3cmd ls` against the target bucket), and for shared project space (`/nesi/project`, `/nesi/nobackup`) confirm `775`/group permissions look sane and warn if not.
3. **Install or update** a `scrontab` entry invoking Tool 1 with these parameters. The entry is tagged with a marker comment so Tool 2 can find and replace its own prior line on re-run, without touching any other entries in the researcher's `scrontab` table. Editing is always read-modify-write against the existing table (`scrontab -l` / `scrontab -e`), never a blind overwrite. Each entry's cron line is preceded by its own `#SCRON --job-name=archive_tool-<id>` directive, so its Slurm job can be told apart from other entries' in `squeue`; add/remove always handle the directive and cron line together.
4. Print a summary
5. **Status command** - archive state (archived, pending, last run, next run) plus retention counts (expired, already deleted) without reading the metadata list by hand. When the base dir is under nobackup and the cleaner's candidate list is readable (login03), it also shows how many pending files are on that list - non-zero means archiving is stuck and those files are about to be deleted. "Next run" comes from `squeue` (a `scrontab` entry's next occurrence is a pending job whose start time is the next run), so it reflects Slurm's own clock and timezone and shows "not scheduled" if Slurm has disabled the entry. Note the controller evaluates schedules in its own timezone (UTC on this cluster: `0 2 * * *` runs at 14:00 NZST).
6. **`--dry-run`** - show what a real run would do (files that would be archived, `scrontab` entry that would be installed/changed) without writing anything.
7. Deleting any other archive (not yet expired) is done by hand via the regular `s3cmd`; the next run notices it's gone and records it.

## Requirements

- Automatically detect data marked finished (on `nobackup`, `project` or `home`), and archive it to Freezer without further manual steps.
- Never lose data to the 90-day `nobackup` auto-cleaner while it's still pending archival.
- Resumable and safe to re-run: no duplicate or corrupt archives from an interrupted run.
- No credentials, accounts, or permissions beyond what the researcher already has for their own project.
- **Permissions:** shared write access within a project directory needs `775` group permissions so collaborators can write without clobbering each other.
- **Local logging:** Tool 1 writes a plain-text/structured log  on every run the source of truth a researcher or support would tail/read directly if something looks wrong.
- **Retention:** archived data kept on tape for at least 2 years by default. Tool 1 warns once a tar is past the configured retention period; deleting it takes a manual `runner_archive … --delete-expired` run (the warning and `status` give the exact command). Nothing is deleted from Freezer unattended.
- **Scheduling:** all unattended runs go through `scrontab`, not regular cron, because regular cron isn't available to users.
- If an already-archived file is later modified, detected drift (recorded state vs. current file) is a log warning at run-time, not a stored metadata entry.
- The Freezer-side copy is otherwise left alone unless the researcher opts in with a manual run - a `--overwrite` flag (never installed into `scrontab`) should make explicit whether a changed source file gets re-archived or just logged. When it does re-archive, the superseded tar is deleted immediately rather than left for retention to age out - any other, untouched files it held are re-bundled into the new tar first so nothing loses its backup. If one of those files has since vanished from disk, the old tar is left alone for retention to clean up later instead, to avoid destroying its only remaining copy.

### Open questions / risks

- **Multiple researchers, one project.** Current design assumes exactly one person sets up, `scrontab`, config changes must be done by same user (note, pattern can specify across whole project).
  If same config needs to be editable by whole project (questionable), there are options.
  - Used user-systemd, while still has to run as user, those files can be given group permissions.(messy)
  - File based config. Scron would read the config at execute time. (still requires someone to set up their own crontab, messy)
  - Globus compute can maybe do something?
- **No dead-man's-switch.** If Tool 1 stops mid run, what happens?
- **Copy mechanism**: whether the tar-and-write-to-Freezer step uses the S3 API (`s3cmd`/similar) or Globus.
- **What "visibility into what's archived" should actually be**: is the metadata file enough, or does this need a log, an email, or a small "check status" command in Tool 1/Tool 2 rather than expecting researchers to read a manifest directly?

### Out of scope, for now
- A generic tool accessable to all researchers, put in `opt-nesi-bin`.
- Tool might push increased use of archive, Freezer's actual write throughput is bounded by a small number of physical tape heads, we would want to make sure that this tool does not interfere with regular usage.
  - **Slurm reservation.** Since every Tool 1 run is already a Slurm job by construction (via `scrontab`), this may be closer to configuration than new infrastructure: have Tool 2 set a reservation on the `#SCRON` directive with a hard cap (e.g. 2 running at once), and Slurm's own scheduler enforces the limit across every project using it no bespoke queue to build or own.
  - **Semaphore.** A small, fixed number of lock files in a well-known shared location (one per available Freezer head). Tool 1 tries to `flock` one before writing, if none are free, it waits or defers to the next scheduled run. Simpler to reason about than a Slurm partition, but it's infrastructure someone still has to place and maintain, and doesn't get fairness/scheduling for free the way Slurm does.
  - **Jitter.** Tool 2 assigns each project a randomised offset within the scheduling window, spreading start times out to reduce collision odds.
- Centralised logging (loki/alloy), best handled seperately to tool IMO.