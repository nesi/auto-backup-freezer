# Manual Test Checklist

Manual, on-cluster tests for `freezer_backup_runner` (Tool 1) and `freezer_backup` (Tool 2), against real `s3cmd`, `scrontab` and Freezer. (!!) marks a suspected bug or an open decision.

---

## How to run these tests

### Before you start

- Both tools on your `PATH`.
- Shell variables used throughout:

```bash
export PATH=/nesi/project/reannz00001/auto-backup-freezer:$PATH
REPO=/nesi/project/reannz00001/auto-backup-freezer
T=/nesi/nobackup/reannz00001/freezer-test/data   # base dir
B=test-guest-collection-11926
P="$T/*_final"                                   # always quote it
```

## Test data

/nesi/nobackup/reannz00001/freezer-test/data/



| Path under `$T` | What it's for |
|---|---|
| `a_final/` | Mostly plain text (`auto` -> `.tar.gz`), plus every awkward case below |
| `a_final/deep/…`, `empty_dir/`, `deep/l1/empty_nested/` | Nesting, empty dirs |
| `a_final/zero_bytes.bin` | 0-byte file |
| `a_final/.hidden`, `.hidden_dir/`, `.git/`, `deep/.freezer/` | Hidden files, and a nested dir named `.freezer` (only the base dir's `.freezer` is excluded) |
| `a_final/names/` | Spaces, unicode, leading `-`, quotes, glob characters, a newline, a non-UTF-8 name, very long names/paths |
| `a_final/links/` | Symlinks: inside, outside (relative and absolute), to a dir, dangling |
| `a_final/hardlink_a.txt`, `hardlink_b.txt` | A hardlink pair |
| `a_final/perms/` | Restrictive modes |
| `b_final/` | Mostly already-compressed (`auto` -> `.tar`), with real magic bytes |
| `c_final/` | Small plain folder with a subdir |
| `big_final/` | 9 GiB incompressible file (> 8 GiB), 10 GiB sparse file |
| `d_wip/`, `final/`, `x_final_old/`, `e_final` (a file) | Must **not** match `*_final` |
| `outside_target.txt` | Target of the "outside" symlinks |

If you do a test, please date and sign your name.


## CLI parsing (both tools)

- [ ] Help (`-h`, `--help`, no args for `freezer_backup`, per-subcommand help) prints usage and exits 0.
- [ ] Missing required options, unknown flags and unknown subcommands give a clear error and exit 2.
- [ ] Unsupported values for each option (`-c`, `-l`, `-r`, `-m`) are rejected with a clear message, exit 2. Valid values are accepted in any case where that makes sense (e.g. `-l debug`).
- [ ] An unquoted, shell-expanded glob is rejected ("unexpected argument(s)"), not quietly treated as the first match.
- [ ] Short and long forms of every flag behave the same.
- [ ] Bucket given with and without `s3://` is treated the same.
- [ ] Relative patterns work, and are stored as absolute paths in scrontab entries.
- [ ] `-m` is accepted and otherwise ignored (it's a stub).
- [ ] Exit codes are consistent: 0 success, 1 runtime failure, 2 usage error. `freezer_backup_runner` exits 1 iff it logged any WARNING/ERROR (incl. s3cmd not configured), and 0 when the lock is held.

## `freezer_backup add`

Use `--no-run` for most of these, so you're testing the scrontab handling rather than archiving.

### Validation

- [ ] Bad base dirs (missing, not writable, glob in a parent component) are rejected with a clean message and no traceback, and scrontab is unchanged.
- [ ] Awkward patterns: spaces, `'`, `%`, `$`, backtick, `;`, `~`, trailing slash. Each is either rejected clearly, or installed so the scheduled run uses it literally (no expansion or injection). (!!) Check whether scrontab treats `%` specially, as cron does. (!!) A quoted `~` gives a confusing "does not exist" error.
- [ ] (!!) `--bucket` is written into the cron line **unquoted**, so `-b 'x;touch /tmp/pwned'` would be executed. Confirm, and decide on quoting or stricter validation. `--mail-user` is now on the `#SCRON` directive (parsed by scrontab, not a shell) - check `-m 'x;touch /tmp/pwned'` and `-m 'a@b --time=99'` do nothing odd.
- [ ] Bucket problems (doesn't exist, no access, s3cmd unconfigured, s3cmd not on PATH) give clear, distinct messages and exit 1.
- [ ] A shared base dir (`/nesi/project`, `/nesi/nobackup`) that isn't group-writable gives a warning, but `add` still succeeds. No warning elsewhere.
- [ ] (!!) An invalid schedule (`-s "not a cron"`, `-s "61 * * * *"`) isn't validated, so scrontab fails with a raw traceback. Check that the existing table isn't damaged.
- [ ] (!!) `-s @daily` installs, but is invisible to `status` and `remove -p`. Note how `remove -a` and re-running `add` treat it.

### Installing and updating

- [ ] First add installs a `#SCRON --job-name` directive plus a marked cron line. Output shows the line and the log and metadata paths. The default schedule is `0 2 * * *`.
- [ ] Only options you gave appear in the line (defaults aren't baked in), and every value round-trips through `status`.
- [ ] The immediate run happens by default and produces log and bucket content. `--no-run` skips it.
- [ ] (!!) The immediate run is silent until it finishes, and runs on the login node. Is that acceptable for a large folder?
- [ ] Re-adding the same pattern (even written differently) prompts, and can be declined, accepted, or skipped with `-y`. When non-interactive without `-y`, it refuses rather than hangs. It always ends up with exactly one entry.
- [ ] Different patterns get independent entries.
- [ ] **Unrelated scrontab content is preserved byte-for-byte** across add/update/remove, including other jobs' `#SCRON` directives and comments.
- [ ] Works on an empty or fresh scrontab.
- [ ] (!!) If `scrontab -l` fails (slurmctld unreachable), the tool reads it as empty and the next write **wipes the user's whole table**. Confirm, and decide on a fix.
- [ ] (!!) A `#DISABLED` line (after `scancel`) disappears from `status`. `add` should restore it without creating a duplicate.
- [ ] (!!) A hand-added `#SCRON` directive above a managed entry gets orphaned on update or remove. Note the behaviour.

### `add --dry-run`

- [ ] Shows "would add" or "would update" plus the runner's preview. It never prompts, and never changes scrontab, the bucket or the metadata.
- [ ] Preview counts match what a real run then archives.
- [ ] (!!) The runner still creates `.freezer/`, the lock and log lines (bumping `status`'s "last run"). Is that acceptable for "writes nothing"?
- [ ] Dry-run while a real run holds the lock says so. (!!) At `-l WARNING` or above that message is filtered out, and the preview is empty.

## `freezer_backup remove`

- [ ] Removing by pattern, by id (any case) and with `--all` works. The prompt can be declined, and `-y` skips it. When non-interactive without `-y`, it refuses.
- [ ] Only managed lines and their directives are removed. Everything else is untouched.
- [ ] Unknown entry, no entries, or no args: clear message and a sensible exit code. (!!) The "no entry" message shows the hashed id, not what you typed.
- [ ] Edge cases worth noting: a relative pattern resolved from a different cwd, and a folder name that happens to be 6 hex chars (treated as an id).
- [ ] (!!) `#DISABLED` or unparseable managed lines can't be removed by `remove -p`. Document the manual fix.
- [ ] Removing an entry leaves `.freezer/` and the Freezer tars intact.

## `freezer_backup status`

- [ ] Shows every documented field per entry, with sensible defaults for omitted options. It filters by pattern or id, and handles unknown entries or no entries cleanly.
- [ ] Archived and expired counts match what's actually in the bucket. (!!) After `--overwrite`, superseded records are double-counted. (!!) The spec's "pending" count isn't shown.
- [ ] Degrades gracefully when things are missing or broken: s3cmd unconfigured, squeue unavailable, metadata missing or corrupt. (!!) With s3cmd not on PATH at all, status crashes with a traceback.
- [ ] "next run" matches `squeue --me`, including while the job is running and after it's been descheduled.
- [ ] (!!) "last run" is the log's mtime, so dry-runs and lock-held runs change it.
- [ ] The `cleaner:` line appears only on login03 for nobackup base dirs.
- [ ] Entries that share a bucket list that bucket only once.

## Folder discovery and pattern matching

- [ ] Only top-level directories matching the glob are processed: not files, not near-miss names, not nested matches.
- [ ] Wildcards (`*`, `?`, `[ab]`) and exact names behave as expected. `*` excludes `.freezer`.
- [ ] A pattern that matches nothing is logged, exits 0, and still runs retention.
- [ ] Note the behaviour for dotdirs, top-level symlinked dirs, and renamed folders (which are re-archived under the new name).
- [ ] (!!) If the base dir is deleted after `add`, the next run recreates it. Should it fail instead?

## Diff and idempotency

- [ ] First run archives everything, one tar per folder. A repeat run archives nothing new.
- [ ] New files are archived alone in a new tar, for that folder only.
- [ ] Same-day and next-day tar naming never overwrites an existing object.
- [ ] Empty folders and empty dirs produce nothing. Deleted or renamed source files behave as documented (no sync).
- [ ] (!!) Losing `metadata.jsonl` and re-running the same day **overwrites** existing Freezer objects.
- [ ] (!!) Three same-day `--overwrite` runs can reuse a deleted tar's name, and the new tar is then ignored by retention forever. Reproduce.
- [ ] Two patterns sharing a base dir share metadata and the lock. See "Retention deletion" and "Concurrency and locking".

## Tar contents and compression

- [ ] Extract a tar and compare it against the source: relative paths, byte-identical content, mtimes, permissions and ownership preserved, and only the top-level `.freezer` excluded.
- [ ] All the awkward names survive the round trip, including non-UTF-8 names (in the tar and in metadata JSON).
- [ ] (!!) A non-UTF-8 name in a *log message* makes the console handler print "--- Logging error ---".
- [ ] Symlinks and hardlinks: note what's stored. (!!) Dir and dangling symlinks are dropped entirely. File symlinks are stored as links, but drift is checked on their target.
- [ ] A > 8 GiB file round-trips. A sparse file is read in full: note the time.
- [ ] `-c auto|always|never` pick `.tar` or `.tar.gz` as documented, including edge cases (all zero-byte files). Note the ratio and time on realistic data.

## Upload and checksum

- [ ] Uploads preserve attributes whatever `~/.s3cfg` says.
- [ ] The recorded checksum matches both `s3cmd info` and a downloaded copy, including for multipart uploads.
- [ ] Every tar has `x-amz-meta-chunksize: 15` (`s3cmd info`), whatever `multipart_chunk_size_mb` is in `~/.s3cfg`. For a multipart tar, `multipart-checksum <downloaded tar> 15` ([auto-checksum-freezer](https://github.com/nesi/auto-checksum-freezer)) matches Freezer's ETag (shown by `s3cmd ls -l`).
- [ ] (!!) A missing MD5 is recorded as `null`, and the files still count as archived. Should that fail instead?
- [ ] The temp tar goes to `$TMPDIR` (or `/tmp`) and is always cleaned up. Check the free space there on login nodes and inside scron jobs.

## Drift and `--overwrite`

- [ ] Modified files are detected and warned about once per folder per run (with a copy-pasteable fix command). Nothing is re-archived without `-o`.
- [ ] With `-o`, the new tar holds the drifted files plus the untouched siblings from the old tar. The old tar is deleted and recorded.
- [ ] If a sibling is missing from disk, or vanishes mid-run, the old tar is kept for retention, with a warning.
- [ ] Multiple old tars are handled independently. `-o --dry-run` previews accurately and writes nothing.
- [ ] Note: `touch` alone counts as drift, and backdated mtimes aren't detected.
- [ ] `freezer_backup add` can't set `--overwrite`, so scheduled runs only warn. Confirm scope.

## Touch (auto-cleaner protection)

Internal, not a flag on either tool. Runs only when the base dir's real path is under `/nesi/nobackup`. A pending file (unarchived, or drifted without `-o`) is touched once the newer of its atime/ctime is `TOUCH_THRESHOLD_DAYS` (60) old. That moves atime and ctime, but not mtime. To fake "now":

```bash
cd $REPO && /bin/python -c "
import importlib.util, time; from pathlib import Path
s = importlib.util.spec_from_file_location('r', '.freezer_backup_runner.py'); r = importlib.util.module_from_spec(s); s.loader.exec_module(r)
r.setup_logging(Path('/dev/null'), 'INFO')
r.touch_pending(Path('$T/a_final'), r.load_metadata(Path('$T/.freezer/metadata.jsonl')), now=time.time() + 61*86400)"
```

- [ ] Only pending files past the threshold are touched. mtime is never changed, and archived files are never touched.
- [ ] Dry-run reports without touching.
- [ ] Files you can't touch (another member's `600` files), or that vanish in the meantime, are reported, and the run continues.
- [ ] Base dirs outside nobackup are never touched. Base dirs reached via a symlink into nobackup are.
- [ ] Recorded mtimes in the tar are the real ones, not the touch time.
- [ ] Confirm the cleaner's rules (atime **and** ctime > 90 days, lists at ~76 days, fortnightly) with NeSI.

## Retention deletion

**Start on day 1.** Freezer's `LastModified` can't be backdated, so use `-r 1` and wait a day. Expired tars are only warned about unless `-d` is given. `freezer_backup add` can't set `-d`.

- [ ] Fresh tars are silent. Expired ones get one summary warning, with a copy-pasteable delete command, and `status` agrees.
- [ ] `-d` deletes each expired tar and records it once. Dry-run previews it. The following run is a no-op.
- [ ] `-r 0` disables retention entirely, even with `-d`.
- [ ] Tars deleted by hand are noticed and recorded as deleted without a `s3cmd del`.
- [ ] Superseded tars from `--overwrite` still age out.
- [ ] Listing or delete failures never cause wrong deletions or wrong events. Note the exit codes.
- [ ] Large buckets (> 1000 objects) are listed completely.
- [ ] (!!) **Timezone**: if `s3cmd ls` dates are UTC, an NZ-morning upload is one day old immediately. Test with `-r 1`.
- [ ] (!!) Two entries with **different buckets in the same base dir**: each records the other's tars as deleted. Reproduce.
- [ ] (!!) Same basename under a bucket "subdirectory" could be confused with a root tar.
- [ ] Confirm with the Freezer admins that `LastModified` survives tape migration and recall.

## Concurrency and locking

- [ ] A second run on the same base dir (same or different pattern, manual or via `add`) reports "lock already held", exits 1 and does nothing else.
- [ ] A killed run (`kill -9`) releases the lock. A leftover lock file is harmless.
- [ ] (!!) A second user who can't open the lock file for writing gets a raw traceback. See "Permissions and multiple users".

## Failure and interruption

- [ ] `SIGTERM` mid-upload (what Slurm sends at the time limit): nothing recorded, and the next run retries. (!!) The abandoned multipart upload leaks (check `s3cmd multipart`/`abortmp`), and so does the temp tar in `$TMPDIR`.
- [ ] (!!) A kill between upload and the metadata write leaves an orphan tar that's overwritten the same day or kept forever.
- [ ] A truncated last metadata line is warned about and recovered from.
- [ ] Upload failures (network, bucket gone) are logged with a traceback and exit 1. (!!) The remaining folders and retention are skipped. (!!) With `~/.s3cfg` missing at run time, the exit code is 0.
- [ ] Files that vanish before or during archiving are skipped without a crash.
- [ ] (!!) A file shrinking mid-tar, or `$TMPDIR` filling up, is treated as "vanished", so a **corrupt or truncated tar may be uploaded** and recorded. Reproduce and inspect the result.
- [ ] (!!) An unreadable file is logged as "vanished" at INFO and silently retried forever. It needs a WARNING and a clearer message.
- [ ] Quota exhausted in `.freezer/` during a metadata write: note the behaviour.

## User environment

Things users could plausibly have in their shell that might change behaviour. The main risks: `PYTHONPATH` leaking into `/bin/python` or s3cmd's `/usr/bin/python3 -s` (`-s` only drops user site), and `PATH` resolving `s3cmd`/`freezer_backup_runner`/`touch` to something else. Worth checking scheduled runs as well as manual ones.

### Python environments

- [ ] **Active venv**.
- [ ] **venv with its own `s3cmd`** (`pip install s3cmd`, possibly a different version): note which `s3cmd` is used, and whether its output (exit codes, `ls` format, `info` MD5 line) still parses.
- [ ] **Active conda/mamba env** (`module load Miniforge3`, `conda activate`), both base and a named env: everything works. Check whether conda's `PYTHONPATH`, `PYTHONHOME` or `LD_LIBRARY_PATH` breaks `/bin/python` or `s3cmd`.
- [ ] **conda env that provides coreutils or its own s3cmd**: note which `touch`/`s3cmd` are picked up, and whether they behave the same.
- [ ] **NeSI Python module loaded** (`module load Python/3.x`): same checks. Modules commonly set `PYTHONPATH` to a different Python version's site-packages.
- [ ] **Running with a different interpreter** (`python .freezer_backup.py …` using a conda 3.12, or an old 3.8): either it works, or it fails with a clear message rather than an obscure one.
- [ ] **`PYTHONPATH` containing stdlib-shadowing modules** (e.g. a dir with `json.py`, `logging/`, `typing.py`, `dataclasses.py`, `tarfile.py`, as left by old backport packages): note whether either tool, or s3cmd, breaks and how obvious the error is.
- [ ] **A `json.py` or `logging.py` in the cwd**: should be unaffected.
- [ ] **`sitecustomize.py` / `usercustomize.py`** or `.pth` files in `~/.local/lib/python3.9/site-packages`: no effect on the tools.
- [ ] **Python behaviour env vars**: `PYTHONWARNINGS=error`, `PYTHONIOENCODING=ascii`, `PYTHONDONTWRITEBYTECODE`, `PYTHONSTARTUP`, `PYTHONSAFEPATH`. None of them change behaviour, or crash on non-ASCII output.

### Command lookup (`PATH`)

- [ ] **A stale or different `freezer_backup_runner` earlier on `PATH`** (an old copy, or a pip package with the same name): `add`'s immediate run and the scron line both call bare `freezer_backup_runner`. Note which copy runs, and whether that's acceptable, or whether the entry should use an absolute path.
- [ ] **`freezer_backup`/`freezer_backup_runner` invoked through a symlink or from another dir**: still works.
- [ ] **Wrapper scripts or shell functions/aliases named `s3cmd`**, e.g. one that adds output or changes the exit code.

### Tool configuration env vars

- [ ] **s3cmd config overrides**: `S3CMD_CONFIG` pointing elsewhere, leftover `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_PROFILE` from other tools, and `http_proxy`/`https_proxy`/`no_proxy`. Note which credentials and endpoint are actually used, and that errors point at the right cause.
- [ ] **Unusual `~/.s3cfg` settings**: e.g. `human_readable_sizes`, `progress_meter`, `preserve_attrs = False`, a small `multipart_chunk_size_mb`, `check_ssl_certificate`. Output parsing and checksums still work.
- [ ] **Slurm output env vars**: `SQUEUE_FORMAT`, `SQUEUE_SORT`, `SQUEUE_STATES`, `SQUEUE_USERS`, `SLURM_TIME_FORMAT`. (!!) Filters like `SQUEUE_STATES=RUNNING` may hide the pending scron job, so `status` would report "not scheduled".
- [ ] **Slurm submission env vars**: `SBATCH_ACCOUNT`, `SBATCH_PARTITION`, `SLURM_ACCOUNT`, etc. Do they leak into the scron job's account or partition?
- [ ] **Running from inside a Slurm job** (`srun --pty bash`, or `salloc`): `add`, `remove` and `status` still work. The scrontab and `squeue --me` results are the same as from the login shell.

### Shell and system settings

- [ ] **Locale**: `LANG=C`, `LC_ALL=POSIX`, and a non-UTF-8 locale (e.g. `en_NZ.ISO-8859-1`). Non-ASCII and non-UTF-8 filenames still archive, log and print without crashing.
- [ ] **`TZ`** set to something other than NZ (e.g. `UTC`): tar names, "last run", "next run" and retention ages are still right.
- [ ] **`TMPDIR`** set to a nonexistent, read-only or tiny directory: a clear error, and no bad upload.
- [ ] **Restrictive `umask`** (`077`) or permissive (`002`): note the resulting `.freezer/` perms and their effect on other project members.
- [ ] **`HOME` different from the login home**, or an unreadable `~/.s3cfg`: clear errors.
- [ ] **Noisy shell startup** (`~/.bashrc` that echoes, runs `module load`, or activates conda): no effect on the scheduled job, or on output parsing.

### The scheduled job's environment

- [ ] Find out which environment a scron job gets: the one active when `freezer_backup add` ran (venv/conda activated), or a clean login environment. Document it, because it decides which `freezer_backup_runner`, `s3cmd` and `PYTHONPATH` the scheduled run uses.
- [ ] Install an entry from inside an activated conda env, then deactivate it, or delete the env. The scheduled run still works.

## Scheduled runs (real `scrontab`)

- [ ] With a temporary `-s "*/10 * * * *"`, the job appears in `squeue --me` under its job name, and runs to completion.
- [ ] `freezer_backup_runner`, `s3cmd` and `~/.s3cfg` are all available inside the scron job, and `/bin/python` is ≥ 3.9 on compute nodes. See "User environment".
- [ ] Find and document where the job's stdout and stderr go.
- [ ] (!!) Only `--job-name` is set, so the job gets the default time limit, memory and account. Does a big folder hit the limit? Should `add` write `-t/--mem/-A`? Which account is charged?
- [ ] A slow run doesn't overlap the next occurrence.
- [ ] `scancel` disables the entry. Document the recovery (`add` again).
- [ ] Restore the real schedule, or `remove` the entry, afterwards.

## Permissions and multiple users

- [ ] `.freezer/` and its files get group-writable perms (per the 775 requirement), and inherit the group under setgid.
- [ ] (!!) A second project member on the same base dir hits a raw traceback if the log or lock isn't group-writable.
- [ ] Confirm the per-user-scrontab limitation is documented (SPEC open question).
- [ ] Collaborators' restrictive umasks: see "Touch" and "Failure and interruption".

## Logging

- [ ] `archive.log` always gets DEBUG. The console respects `-l`, including in `add`'s immediate run.
- [ ] Unexpected exceptions are logged with a full traceback.
- [ ] (!!) Gaps: real runs don't log which folders matched, or a per-folder new-file count.
- [ ] (!!) The log grows unbounded, with no rotation. Decide on rotation.

## Mail

A run that logs any WARNING/ERROR exits 1, so the entry's `#SCRON ... --mail-type=FAIL --mail-user=X` mails X. Under its `freezer_backup-<id>` job, `freezer_backup_runner` first puts those messages in the job comment (the mail body) and sleeps 5s.

Already checked with plain `sbatch` jobs (2026-09-25): `scontrol update Comment=` keeps newlines, quotes/`$`/backticks and unicode; 1024 chars is accepted, 1025+ is rejected outright ("too long", comment left empty), hence the 1024-byte cap. An end-to-end `freezer_backup_runner --dry-run` with 12 drift warnings stored a 981-byte comment (3 warnings + "... and 9 more").

- [ ] (!!) scrontab accepts several options on one `#SCRON` line (`--job-name=... --mail-type=FAIL --mail-user=...`): `add -m`, then check `scontrol show job` on the pending job shows the MailUser/MailType.
- [ ] The mail arrives, and its body is the comment, with the line breaks intact.
- [ ] A clean run sends no mail. A run where the lock is held sends no mail.
- [ ] `add` without `-m`, then with, then without again: the directive gains/loses the mail options, `status` shows it.
- [ ] A hand run (login node or inside an interactive `srun`) doesn't touch any job comment and doesn't sleep.
- [ ] Unexpected exception: the mail shows `ERROR unexpected error ...` without the traceback, which is in `archive.log`.
- [ ] Two entries on the same schedule both failing: confirm the rate limiter merges them into one subjects-only mail (no comments).
