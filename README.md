# auto-backup-freezer

Automated archiving from `nobackup` to Freezer for `uoa03387`. See [SPEC.md](SPEC.md) for the design and [PLAN.md](PLAN.md) for implementation status.

- `runner_archive.py` - Tool 1, the unattended archiver (runs via `scrontab`)
- `archive_tool.py` - Tool 2, the `add`/`remove`/`status` setup CLI

## Tests

Stdlib only, no pytest:

```
python3 -m unittest discover -s tests
```
