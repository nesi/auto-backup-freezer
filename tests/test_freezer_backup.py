"""Tests for freezer_backup."""

import gzip
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# .freezer_backup.py is hidden (leading dot), so it can't be imported by name
_spec = importlib.util.spec_from_file_location("freezer_backup", Path(__file__).resolve().parent.parent / ".freezer_backup.py")
tool = sys.modules["freezer_backup"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

REAL_SCHEDULED_RUNS = tool.scheduled_runs
REAL_SLURM_ACCOUNTS = tool.slurm_accounts
_squeue_patch = patch.object(tool, "scheduled_runs", return_value={})
_accounts_patch = patch.object(tool, "slurm_accounts", return_value=(["proj00001"], "proj00001"))


def setUpModule():
    # Keep every status/format_status test from shelling out to the real
    # squeue, and every add from the real scontrol; ScheduledRunsTests and
    # SlurmAccountsTests exercise the real functions directly.
    _squeue_patch.start()
    _accounts_patch.start()


def tearDownModule():
    _squeue_patch.stop()
    _accounts_patch.stop()


class EntryIdentityTests(unittest.TestCase):
    def test_same_pattern_gives_same_id(self):
        self.assertEqual(tool.entry_id("/nb/*_final"), tool.entry_id("/nb/*_final"))

    def test_different_glob_gives_different_id(self):
        self.assertNotEqual(tool.entry_id("/nb/*_final"), tool.entry_id("/nb/*_done"))

    def test_different_base_dir_gives_different_id(self):
        self.assertNotEqual(tool.entry_id("/nb/a/*_final"), tool.entry_id("/nb/b/*_final"))

    def test_id_is_six_hex_characters(self):
        id_ = tool.entry_id("/nb/*_final")
        self.assertEqual(tool.ID_LENGTH, 6)
        self.assertEqual(len(id_), 6)
        self.assertRegex(id_, r"^[0-9a-f]{6}$")


class ResolveToIdTests(unittest.TestCase):
    def test_a_real_pattern_is_hashed(self):
        self.assertEqual(tool.resolve_to_id("/nb/*_final"), tool.entry_id("/nb/*_final"))

    def test_a_short_id_is_passed_through(self):
        id_ = tool.entry_id("/nb/*_final")
        self.assertEqual(tool.resolve_to_id(id_), id_)

    def test_a_short_id_is_case_insensitive(self):
        id_ = tool.entry_id("/nb/*_final")
        self.assertEqual(tool.resolve_to_id(id_.upper()), id_)

    def test_relative_pattern_still_resolves_against_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                result = tool.resolve_to_id("sub/*_final")
            finally:
                os.chdir(original_cwd)
            self.assertEqual(result, tool.entry_id(str(Path(tmp) / "sub" / "*_final")))

    def test_something_that_merely_looks_hex_but_is_the_wrong_length_is_a_pattern(self):
        # only an exact ID_LENGTH-char hex string is treated as an id -
        # anything else (even if hex-looking) is resolved as a pattern.
        result = tool.resolve_to_id("abc")
        self.assertNotEqual(result, "abc")


class IdCollisionTests(unittest.TestCase):
    def test_no_collision_when_no_existing_entries(self):
        with patch.object(tool, "read_scrontab", return_value=""):
            tool.check_id_collision("/nb/*_final")  # must not raise

    def test_no_collision_when_same_pattern_already_exists(self):
        # re-adding the *same* pattern is an update, not a collision.
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab"):
            entry = tool.build_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        with patch.object(tool, "read_scrontab", return_value=entry):
            tool.check_id_collision("/nb/*_final")  # must not raise

    def test_raises_on_a_genuine_collision(self):
        # Simulate a hash collision directly: an existing entry for
        # /nb/a/*_final under a fixed id, and entry_id() patched to
        # return that same id for any pattern - so checking a genuinely
        # *different* pattern ("/nb/b/*_final") against it must raise.
        fixed_id = "abc123"
        existing_line = (
            "0 2 * * * freezer_backup_runner --pattern '/nb/a/*_final' --bucket b --compress auto "
            "--retention-days 730 "
            f"{tool.marker_for(fixed_id)}\n"
        )
        with patch.object(tool, "read_scrontab", return_value=existing_line), \
             patch.object(tool, "entry_id", return_value=fixed_id):
            with self.assertRaises(ValueError):
                tool.check_id_collision("/nb/b/*_final")

    def test_add_or_update_entry_propagates_collision_error(self):
        fixed_id = "abc123"
        existing_line = (
            "0 2 * * * freezer_backup_runner --pattern '/nb/a/*_final' --bucket b --compress auto "
            "--retention-days 730 "
            f"{tool.marker_for(fixed_id)}\n"
        )
        with patch.object(tool, "read_scrontab", return_value=existing_line), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "entry_id", return_value=fixed_id):
            with self.assertRaises(ValueError):
                tool.add_or_update_entry("0 2 * * *", "/nb/b/*_final", "b", "auto")
        write.assert_not_called()


class ReadScrontabTests(unittest.TestCase):
    def _completed(self, stdout="", stderr="", returncode=0):
        return type("CompletedProcess", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode})()

    def test_returns_table(self):
        with patch.object(tool.subprocess, "run", return_value=self._completed("0 5 * * * job\n")):
            self.assertEqual(tool.read_scrontab(), "0 5 * * * job\n")

    def test_no_table_reads_as_empty(self):
        # real scrontab prints this on stdout and exits 1 when the user has never had a table
        with patch.object(tool.subprocess, "run",
                          return_value=self._completed("no crontab for someone\n", returncode=1)):
            self.assertEqual(tool.read_scrontab(), "")

    def test_other_failure_raises(self):
        with patch.object(tool.subprocess, "run",
                          return_value=self._completed(stderr="slurm_load_jobs error\n", returncode=1)):
            with self.assertRaises(RuntimeError):
                tool.read_scrontab()


class ScrontabEntryTests(unittest.TestCase):
    @patch.object(tool, "read_scrontab", return_value="")
    @patch.object(tool, "write_scrontab")
    def test_add_adds_one_marked_line(self, write, _read):
        tool.add_or_update_entry("0 2 * * *", "/some/nobackup/*_final", "mybucket", "auto")
        written = write.call_args[0][0]
        self.assertEqual(written.count(tool.MARKER_PREFIX), 1)
        self.assertIn("mybucket", written)

    def test_pattern_is_quoted_against_shell_glob_expansion(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        written = write.call_args[0][0]
        self.assertIn("'/nb/*_final'", written)

    def test_touch_days_is_never_written(self):
        # Touching is an internal detail of freezer_backup_runner, not an entry setting.
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        self.assertNotIn("--touch-days", write.call_args[0][0])

    def test_job_name_directive_is_written_directly_above_the_entry(self):
        with patch.object(tool, "read_scrontab", return_value="0 5 * * * other_job\n"), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b")
        lines = write.call_args[0][0].splitlines()
        id_ = tool.entry_id("/nb/*_final")
        self.assertEqual(lines[0], "0 5 * * * other_job")
        self.assertEqual(lines[1], f"#SCRON --job-name=freezer_backup-{id_}")
        self.assertIn(tool.marker_for(id_), lines[2])

    def test_rerun_keeps_exactly_one_directive(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b")
        first = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=first), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 3 * * *", "/nb/*_final", "b")
        self.assertEqual(write.call_args[0][0].count("#SCRON"), 1)

    def test_directive_is_not_glued_onto_a_last_line_missing_its_newline(self):
        with patch.object(tool, "read_scrontab", return_value="0 5 * * * other_job"), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b")
        self.assertEqual(write.call_args[0][0].splitlines()[0], "0 5 * * * other_job")

    def test_remove_entry_drops_its_directive_but_not_others(self):
        with patch.object(tool, "read_scrontab", return_value="#SCRON --time=5\n0 5 * * * other_job\n"), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a/*_final", "b")
        table = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=table), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/b/*_final", "b")
        table = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=table), \
             patch.object(tool, "write_scrontab") as write:
            tool.remove_entry(tool.entry_id("/nb/a/*_final"))
        remaining = write.call_args[0][0]
        self.assertNotIn(tool.job_name_for(tool.entry_id("/nb/a/*_final")), remaining)
        self.assertIn(tool.job_name_for(tool.entry_id("/nb/b/*_final")), remaining)
        self.assertTrue(remaining.startswith("#SCRON --time=5\n0 5 * * * other_job\n"))

    def test_remove_all_entries_drops_every_directive_but_not_unrelated_ones(self):
        with patch.object(tool, "read_scrontab", return_value="#SCRON --time=5\n0 5 * * * other_job\n"), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a/*_final", "b")
        table = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=table), \
             patch.object(tool, "write_scrontab") as write:
            tool.remove_all_entries()
        self.assertEqual(write.call_args[0][0], "#SCRON --time=5\n0 5 * * * other_job\n")

    def test_rerun_same_pattern_replaces_not_duplicates(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "old", "auto")
        existing = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 3 * * *", "/nb/*_final", "new", "auto")
        written = write2.call_args[0][0]

        self.assertEqual(written.count(tool.MARKER_PREFIX), 1)
        self.assertIn("--bucket new", written)
        self.assertNotIn("--bucket old", written)

    def test_different_pattern_adds_second_entry(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a/*_final", "b", "auto")
        after_first = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=after_first), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 2 * * *", "/nb/b/*_final", "b", "auto")
        written = write2.call_args[0][0]

        self.assertEqual(written.count(tool.MARKER_PREFIX), 2)
        self.assertIn("/nb/a/*_final", written)
        self.assertIn("/nb/b/*_final", written)

    def test_retention_days_default_is_omitted_when_not_specified(self):
        # Matches freezer_backup_runner's own default (see DEFAULT_RETENTION_DAYS)
        # - build_entry() skips writing it since omitting is equivalent.
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        written = write.call_args[0][0]
        self.assertNotIn("--retention-days", written)

    def test_non_default_retention_days_is_embedded(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto", retention_days=90)
        written = write.call_args[0][0]
        self.assertIn("--retention-days 90", written)

    def test_retention_days_is_included_when_specified(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto", retention_days=365)
        written = write.call_args[0][0]
        self.assertIn("--retention-days 365", written)

    def test_add_preserves_unrelated_entries(self):
        existing = "0 1 * * * /some/other/job\n"
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        written = write.call_args[0][0]
        self.assertIn("/some/other/job", written)

    def test_remove_entry_removes_only_matching_pattern(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a/*_final", "b", "auto")
        after_a = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=after_a), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 2 * * *", "/nb/b/*_final", "b", "auto")
        after_both = write2.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=after_both), \
             patch.object(tool, "write_scrontab") as write3:
            tool.remove_entry(tool.entry_id("/nb/a/*_final"))
        written = write3.call_args[0][0]

        self.assertNotIn("/nb/a/*_final", written)
        self.assertIn("/nb/b/*_final", written)

    def test_remove_all_entries_removes_everything_managed_but_not_unrelated(self):
        existing = (
            "0 1 * * * /some/other/job\n"
            f"0 2 * * * freezer_backup_runner --pattern '/nb/a/*_final' --bucket b --compress auto {tool.marker_for(tool.entry_id('/nb/a/*_final'))}\n"
            f"0 2 * * * freezer_backup_runner --pattern '/nb/b/*_final' --bucket b --compress auto {tool.marker_for(tool.entry_id('/nb/b/*_final'))}\n"
        )
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write:
            tool.remove_all_entries()
        written = write.call_args[0][0]
        self.assertNotIn(tool.MARKER_PREFIX, written)
        self.assertIn("/some/other/job", written)


class MailUserTests(unittest.TestCase):
    """mail_user goes on the entry's #SCRON directive (Slurm mails on FAIL), not the cron line."""

    def test_directive_without_mail_user_is_just_the_job_name(self):
        self.assertEqual(tool.directive_for("abc123"), "#SCRON --job-name=freezer_backup-abc123\n")

    def test_directive_with_mail_user_mails_on_failure(self):
        self.assertEqual(tool.directive_for("abc123", "a@example.com"),
                         "#SCRON --job-name=freezer_backup-abc123 --mail-type=FAIL --mail-user=a@example.com\n")

    def test_cron_line_never_carries_mail_user(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto", mail_user="a@example.com")
        lines = write.call_args[0][0].splitlines()
        self.assertEqual(lines[0], tool.directive_for(tool.entry_id("/nb/*_final"), "a@example.com").strip())
        self.assertNotIn("mail", lines[1])
        self.assertEqual(len(lines), 2)

    def test_rerun_with_new_mail_user_replaces_old_not_duplicates(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto", mail_user="old@example.com")
        existing = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto", mail_user="new@example.com")
        written = write2.call_args[0][0]

        self.assertEqual(written.count(tool.MARKER_PREFIX), 1)
        self.assertEqual(written.count("#SCRON"), 1)
        self.assertIn("new@example.com", written)
        self.assertNotIn("old@example.com", written)

    def test_rerun_dropping_mail_user_removes_it(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto", mail_user="a@example.com")
        existing = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        written = write2.call_args[0][0]

        self.assertNotIn("mail", written)
        self.assertEqual(written.count("#SCRON"), 1)

    def test_remove_entry_removes_a_mail_configured_entry_cleanly(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a/*_final", "b", "auto", mail_user="a@example.com")
        after_a = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=after_a), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 2 * * *", "/nb/b/*_final", "b", "auto")
        after_both = write2.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=after_both), \
             patch.object(tool, "write_scrontab") as write3:
            tool.remove_entry(tool.entry_id("/nb/a/*_final"))
        written = write3.call_args[0][0]
        self.assertNotIn("/nb/a/*_final", written)
        self.assertNotIn("mail", written)
        self.assertIn("/nb/b/*_final", written)

    def test_list_entries_captures_mail_user_from_the_directive(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a/*_final", "b", mail_user="a@example.com")
        after_a = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=after_a), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 2 * * *", "/nb/b/*_final", "b")
        with patch.object(tool, "read_scrontab", return_value=write2.call_args[0][0]):
            entries = tool.list_entries()
        by_pattern = {e["pattern"]: e for e in entries}
        self.assertEqual(by_pattern["/nb/a/*_final"]["mail_user"], "a@example.com")
        self.assertIsNone(by_pattern["/nb/b/*_final"]["mail_user"])


class BuildEntryDeduplicationTests(unittest.TestCase):
    """build_entry() only writes a flag the researcher actually specified (None means "not specified")."""

    def test_unspecified_compress_mode_is_omitted(self):
        entry = tool.build_entry("0 2 * * *", "/nb/*_final", "b", None)
        self.assertNotIn("--compress", entry)

    def test_specified_compress_mode_is_embedded_even_if_it_matches_the_default(self):
        entry = tool.build_entry(
            "0 2 * * *", "/nb/*_final", "b", tool.DEFAULT_COMPRESS_MODE,
        )
        self.assertIn(f"--compress {tool.DEFAULT_COMPRESS_MODE}", entry)

    def test_unspecified_retention_days_is_omitted(self):
        entry = tool.build_entry("0 2 * * *", "/nb/*_final", "b", "auto", retention_days=None)
        self.assertNotIn("--retention-days", entry)

    def test_specified_retention_days_is_embedded_even_if_it_matches_the_default(self):
        entry = tool.build_entry(
            "0 2 * * *", "/nb/*_final", "b", "auto", retention_days=tool.DEFAULT_RETENTION_DAYS,
        )
        self.assertIn(f"--retention-days {tool.DEFAULT_RETENTION_DAYS}", entry)

    def test_all_unspecified_gives_a_minimal_line(self):
        entry = tool.build_entry("0 2 * * *", "/nb/*_final", "b")
        self.assertEqual(
            entry,
            f"0 2 * * * freezer_backup_runner --pattern '/nb/*_final' --bucket b "
            f"{tool.marker_for(tool.entry_id('/nb/*_final'))}\n",
        )

    def test_list_entries_leaves_omitted_fields_as_none(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b")
        written = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=written):
            entries = tool.list_entries()

        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertIsNone(e["compress"])
        self.assertIsNone(e["retention_days"])

    def test_format_status_resolves_omitted_fields_to_runner_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                "id": "abc123", "pattern": str(Path(tmp) / "*_final"), "bucket": "b",
                "schedule": "0 2 * * *", "compress": None,
                "retention_days": None,
            }
            output = tool.format_status(entry, runs={})
        self.assertIn(f"compress:    {tool.DEFAULT_COMPRESS_MODE}", output)
        self.assertIn(f"log level:   {tool.DEFAULT_LOG_LEVEL}", output)
        self.assertNotIn("touch", output)
        self.assertIn(f"retention:   {tool.DEFAULT_RETENTION_DAYS} day(s)", output)


class ListAndStatusTests(unittest.TestCase):
    def test_list_entries_parses_pattern_bucket_compress_schedule(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "mybucket", "always")
        existing = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=existing):
            entries = tool.list_entries()

        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["pattern"], "/nb/*_final")
        self.assertEqual(e["bucket"], "mybucket")
        self.assertEqual(e["compress"], "always")
        self.assertEqual(e["schedule"], "0 2 * * *")
        self.assertIsNone(e["retention_days"])

    def test_file_records_counts_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            path.write_text('{"folder": "x", "file": "a"}\n{"folder": "x", "file": "b"}\n')
            self.assertEqual(len(tool.file_records(path)), 2)

    def test_file_records_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does_not_exist.jsonl"
            self.assertEqual(len(tool.file_records(path)), 0)

    def test_file_records_excludes_deleted_events(self):
        # A retention `deleted` event is an append-only audit record, not
        # an archived file - it shouldn't inflate the count past the
        # number of files actually archived.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            path.write_text(
                '{"folder": "x", "file": "a"}\n'
                '{"folder": "x", "file": "b"}\n'
                '{"event": "deleted", "tar_name": "x-old.tar", "date": "2026-01-01"}\n'
            )
            self.assertEqual(len(tool.file_records(path)), 2)

    def test_read_records_warns_on_unparseable_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            path.write_text('{"folder": "x", "file": "a"}\nnot json at all\n')
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                records = tool.read_records(path)
        self.assertEqual(len(records), 1)
        self.assertIn("unparseable", mock_stderr.getvalue())

    def test_last_activity_never_when_no_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.log"
            self.assertEqual(tool.last_activity(path), "never")


class RetentionStatusTests(unittest.TestCase):
    def _completed(self, stdout="", returncode=0):
        return type("CompletedProcess", (), {"stdout": stdout, "returncode": returncode})()

    def _write_metadata(self, path, records):
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def test_list_bucket_dates_parses_dates(self):
        stdout = (
            "2026-01-01 12:00  1234  s3://bucket/a-20260101.tar\n"
            "2026-02-02 12:00  5678  s3://bucket/b-20260202.tar.gz\n"
        )
        with patch.object(tool.subprocess, "run", return_value=self._completed(stdout)):
            objects = tool.list_bucket_dates("bucket")
        self.assertEqual(objects["a-20260101.tar"].isoformat(), "2026-01-01")
        self.assertEqual(objects["b-20260202.tar.gz"].isoformat(), "2026-02-02")

    def test_list_bucket_dates_returns_none_on_failure(self):
        with patch.object(tool.subprocess, "run", return_value=self._completed("", returncode=1)):
            self.assertIsNone(tool.list_bucket_dates("bucket"))

    def test_retention_status_disabled_returns_none_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            self._write_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old.tar"},
            ])
            expired, deleted = tool.retention_status(metadata_path, "bucket", 0)
            self.assertIsNone(expired)
            self.assertEqual(deleted, 0)

    def test_retention_status_no_archived_tars_returns_empty_expired(self):
        # Nothing archived yet is not a listing failure - should read as
        # "0 expired", not "unknown".
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            expired, deleted = tool.retention_status(metadata_path, "bucket", 730)
            self.assertEqual(expired, [])
            self.assertEqual(deleted, 0)

    def test_retention_status_listing_failure_returns_none_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            self._write_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old.tar"},
            ])
            with patch.object(tool.subprocess, "run", return_value=self._completed("", returncode=1)):
                expired, deleted = tool.retention_status(metadata_path, "bucket", 730)
            self.assertIsNone(expired)
            self.assertEqual(deleted, 0)

    def test_retention_status_lists_expired_and_counts_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            self._write_metadata(metadata_path, [
                {"folder": "sample_final", "file": "old.bin", "tar_name": "old.tar"},
                {"folder": "sample_final", "file": "boundary.bin", "tar_name": "boundary.tar"},
                {"folder": "sample_final", "file": "young.bin", "tar_name": "young.tar"},
                {"folder": "sample_final", "file": "missing.bin", "tar_name": "missing.tar"},
                {"folder": "sample_final", "file": "gone.bin", "tar_name": "gone.tar"},
                {"event": "deleted", "tar_name": "gone.tar", "date": "2020-01-01"},
            ])
            today = datetime.now().date()
            stdout = "".join(
                f"{(today - timedelta(days=days)).isoformat()} 12:00  1234  s3://bucket/{name}\n"
                for name, days in (("old.tar", 800), ("boundary.tar", 730), ("young.tar", 729))
            )
            with patch.object(tool.subprocess, "run", return_value=self._completed(stdout)):
                expired, deleted = tool.retention_status(metadata_path, "bucket", retention_days=730)
            # old.tar past retention, boundary.tar exactly at it (counts),
            # young.tar one day short. missing.tar is absent from the listing
            # (deleted by hand) and gone.tar is recorded deleted - both count
            # as deleted, neither as expired.
            self.assertEqual(expired, [("boundary.tar", 730), ("old.tar", 800)])
            self.assertEqual(deleted, 2)

    def test_format_status_shows_expired_count_and_the_command_to_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                "id": "abc123", "pattern": str(Path(tmp) / "*_final"), "bucket": "b",
                "schedule": "0 2 * * *", "retention_days": "365",
            }
            with patch.object(tool, "retention_status", return_value=([("old.tar", 800)], 3)):
                output = tool.format_status(entry)
        self.assertIn("retention:   365 day(s)\n", output)
        self.assertIn("expired:     1 past retention, 3 already deleted", output)
        self.assertIn(f"to delete: {tool.manual_command(entry, '--delete-expired')}", output)
        self.assertIn("--retention-days 365 --delete-expired", output)

    def test_format_status_includes_retention_line(self):
        entry = {
            "id": "abc123", "pattern": "/nb/*_final", "bucket": "b",
            "schedule": "0 2 * * *", "compress": "auto",
            "retention_days": "0",
        }
        with tempfile.TemporaryDirectory() as tmp:
            entry["pattern"] = str(Path(tmp) / "*_final")
            output = tool.format_status(entry)
        self.assertIn("retention:   disabled", output)

    def test_format_status_shows_mail_not_configured_when_absent(self):
        entry = {
            "id": "abc123", "pattern": "/nb/*_final", "bucket": "b",
            "schedule": "0 2 * * *", "compress": "auto",
            "retention_days": "0", "mail_user": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            entry["pattern"] = str(Path(tmp) / "*_final")
            output = tool.format_status(entry)
        self.assertIn("mail:        not configured", output)

    def test_format_status_shows_configured_mail_address(self):
        entry = {
            "id": "abc123", "pattern": "/nb/*_final", "bucket": "b",
            "schedule": "0 2 * * *", "compress": "auto",
            "retention_days": "0", "mail_user": "a@example.com",
        }
        with tempfile.TemporaryDirectory() as tmp:
            entry["pattern"] = str(Path(tmp) / "*_final")
            output = tool.format_status(entry)
        self.assertIn("mail:        a@example.com\n", output)

    def test_format_status_shows_log_level_independently_of_mail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {"id": "abc123", "pattern": str(Path(tmp) / "*_final"), "bucket": "b",
                    "schedule": "0 2 * * *", "retention_days": "0"}
            no_mail = tool.format_status({**base, "log_level": "DEBUG"})
            default = tool.format_status({**base, "mail_user": "a@example.com"})
        self.assertIn("log level:   DEBUG", no_mail)
        self.assertIn("mail:        not configured", no_mail)
        self.assertIn(f"log level:   {tool.DEFAULT_LOG_LEVEL}", default)
        self.assertIn("mail:        a@example.com\n", default)

    def test_format_status_defaults_to_not_configured_when_key_missing(self):
        # entry dicts built by hand elsewhere (or an older scrontab entry
        # never re-parsed) may not carry a "mail_user" key at all.
        entry = {
            "id": "abc123", "pattern": "/nb/*_final", "bucket": "b",
            "schedule": "0 2 * * *", "compress": "auto",
            "retention_days": "0",
        }
        with tempfile.TemporaryDirectory() as tmp:
            entry["pattern"] = str(Path(tmp) / "*_final")
            output = tool.format_status(entry)
        self.assertIn("mail:        not configured", output)

    def test_bucket_cache_avoids_a_repeat_listing_for_the_same_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            self._write_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "a.tar"},
            ])
            cache = {}
            with patch.object(tool, "list_bucket_dates", return_value={}) as list_dates:
                tool.retention_status(metadata_path, "bucket", 730, bucket_cache=cache)
                tool.retention_status(metadata_path, "bucket", 730, bucket_cache=cache)
            list_dates.assert_called_once_with("bucket")

    def test_bucket_cache_still_fetches_separately_for_different_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            self._write_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "a.tar"},
            ])
            cache = {}
            with patch.object(tool, "list_bucket_dates", return_value={}) as list_dates:
                tool.retention_status(metadata_path, "bucket-a", 730, bucket_cache=cache)
                tool.retention_status(metadata_path, "bucket-b", 730, bucket_cache=cache)
            self.assertEqual(list_dates.call_count, 2)

    def test_bucket_cache_treats_prefixed_and_bare_bucket_as_the_same_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            self._write_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "a.tar"},
            ])
            cache = {}
            with patch.object(tool, "list_bucket_dates", return_value={}) as list_dates:
                tool.retention_status(metadata_path, "bucket", 730, bucket_cache=cache)
                tool.retention_status(metadata_path, "s3://bucket", 730, bucket_cache=cache)
            list_dates.assert_called_once()

    def test_no_cache_given_fetches_every_time(self):
        # bucket_cache=None (the default) - format_status()'s single-entry
        # `status --pattern X` path, where caching would be pointless.
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            self._write_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "a.tar"},
            ])
            with patch.object(tool, "list_bucket_dates", return_value={}) as list_dates:
                tool.retention_status(metadata_path, "bucket", 730)
                tool.retention_status(metadata_path, "bucket", 730)
            self.assertEqual(list_dates.call_count, 2)


class BucketUriTests(unittest.TestCase):
    def test_bucket_without_prefix(self):
        self.assertEqual(tool.bucket_uri("mybucket"), "s3://mybucket/")

    def test_bucket_with_prefix_is_not_doubled(self):
        self.assertEqual(tool.bucket_uri("s3://mybucket"), "s3://mybucket/")

    def test_bucket_with_uppercase_prefix(self):
        self.assertEqual(tool.bucket_uri("S3://mybucket"), "s3://mybucket/")

    def test_with_key(self):
        self.assertEqual(tool.bucket_uri("s3://mybucket", "foo.tar"), "s3://mybucket/foo.tar")

    def test_validate_accepts_prefixed_bucket(self):
        completed = type("CompletedProcess", (), {"stdout": "", "stderr": "", "returncode": 0})()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(tool.subprocess, "run", return_value=completed) as run:
            tool.validate(f"{tmp}/*_final", "s3://mybucket")
        self.assertEqual(run.call_args[0][0], ["s3cmd", "ls", "s3://mybucket/"])


class SplitPatternTests(unittest.TestCase):
    def test_splits_base_dir_and_glob(self):
        base_dir, glob_expr = tool.split_pattern("/nesi/nobackup/uoa03387/*_final")
        self.assertEqual(base_dir, Path("/nesi/nobackup/uoa03387"))
        self.assertEqual(glob_expr, "*_final")


class CmdAddIntegrationTests(unittest.TestCase):
    def test_cmd_add_never_writes_touch_days(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"):
            tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--schedule", "0 2 * * 0"])
        written = write.call_args[0][0]
        self.assertNotIn("--touch-days", written)

    def test_cmd_add_reports_validate_failure_cleanly_instead_of_crashing(self):
        # validate() raises ValueError for a bad pattern/bucket - cmd_add()
        # must turn that into a clean error + nonzero return, not an
        # uncaught traceback.
        with patch.object(tool, "write_scrontab") as write:
            result = tool.cmd_add(["--pattern", "/definitely/does/not/exist/*_final", "--bucket", "b"])
        self.assertEqual(result, 1)
        write.assert_not_called()

    def test_cmd_add_dry_run_does_not_write_scrontab(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"):
            result = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--dry-run"])
        self.assertEqual(result, 0)
        write.assert_not_called()

    def test_cmd_add_dry_run_reports_add_for_new_pattern(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab"), \
             patch.object(tool, "run_archive"), \
             patch("builtins.print") as mock_print:
            tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--dry-run"])
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("would add", printed)

    def test_cmd_add_dry_run_reports_update_for_existing_pattern(self):
        existing_line = tool.build_entry("0 2 * * *", "/nb/*_final", "old-bucket", "auto")
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=existing_line), \
             patch.object(tool, "write_scrontab"), \
             patch.object(tool, "run_archive"), \
             patch("builtins.print") as mock_print:
            tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--dry-run"])
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("would update", printed)

    def test_cmd_add_dry_run_previews_freezer_backup_runner(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab"), \
             patch.object(tool, "run_archive") as run_archive:
            tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--dry-run"])
        run_archive.assert_called_once()
        self.assertTrue(run_archive.call_args.kwargs["dry_run"])

    def test_cmd_add_with_mail_user_writes_it_on_the_directive(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"):
            tool.cmd_add([
                "--pattern", "/nb/*_final", "--bucket", "b", "--mail-user", "a@example.com",
            ])
        written = write.call_args[0][0]
        self.assertIn(tool.directive_for(tool.entry_id("/nb/*_final"), "a@example.com"), written)

    def test_cmd_add_without_mail_user_writes_only_the_job_name_directive(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"):
            tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b"])
        scron_lines = [l for l in write.call_args[0][0].splitlines() if l.startswith("#SCRON")]
        self.assertEqual(scron_lines, [tool.directive_for(tool.entry_id("/nb/*_final")).strip()])

    def test_cmd_add_runs_archive_immediately_by_default(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab"), \
             patch.object(tool, "run_archive") as run_archive:
            tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b"])
        run_archive.assert_called_once()
        self.assertFalse(run_archive.call_args.kwargs["dry_run"])

    def test_cmd_add_no_run_skips_the_immediate_run(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab"), \
             patch.object(tool, "run_archive") as run_archive:
            tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--no-run"])
        run_archive.assert_not_called()

    def test_cmd_add_no_run_still_writes_the_scrontab_entry(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"):
            result = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--no-run"])
        self.assertEqual(result, 0)
        write.assert_called_once()

    def test_cmd_add_new_pattern_does_not_prompt(self):
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"), \
             patch("builtins.input") as mock_input:
            result = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--no-run"])
        self.assertEqual(result, 0)
        write.assert_called_once()
        mock_input.assert_not_called()

    def test_cmd_add_existing_pattern_with_yes_does_not_prompt(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "old-bucket", "auto")
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"), \
             patch("builtins.input") as mock_input:
            result = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--no-run", "--yes"])
        self.assertEqual(result, 0)
        write.assert_called_once()
        mock_input.assert_not_called()

    def test_cmd_add_existing_pattern_confirmed_overwrites(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "old-bucket", "auto")
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="y"):
            result = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--no-run"])
        self.assertEqual(result, 0)
        write.assert_called_once()
        self.assertIn("--bucket b", write.call_args[0][0])

    def test_cmd_add_existing_pattern_declined_does_not_overwrite(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "old-bucket", "auto")
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="n"):
            result = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--no-run"])
        self.assertEqual(result, 1)
        write.assert_not_called()

    def test_cmd_add_existing_pattern_non_interactive_refuses_without_prompting(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "old-bucket", "auto")
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("builtins.input") as mock_input:
            result = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--no-run"])
        self.assertEqual(result, 1)
        write.assert_not_called()
        mock_input.assert_not_called()

    def test_cmd_add_dry_run_never_prompts_even_for_an_existing_pattern(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "old-bucket", "auto")
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"), \
             patch("builtins.input") as mock_input:
            result = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--dry-run"])
        self.assertEqual(result, 0)
        write.assert_not_called()
        mock_input.assert_not_called()


class ConfirmTests(unittest.TestCase):
    def test_interactive_yes_confirms(self):
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="y"), \
             patch("builtins.print"):
            self.assertTrue(tool._confirm("add", ["some context"], "proceed?"))

    def test_interactive_blank_input_declines(self):
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value=""), \
             patch("builtins.print"):
            self.assertFalse(tool._confirm("add", ["some context"], "proceed?"))

    def test_non_interactive_refuses_without_calling_input(self):
        with patch("sys.stdin.isatty", return_value=False), \
             patch("builtins.input") as mock_input, \
             patch("builtins.print"):
            self.assertFalse(tool._confirm("add", ["some context"], "proceed?"))
        mock_input.assert_not_called()


class RunArchiveTests(unittest.TestCase):
    def _completed(self, stdout="", stderr="", returncode=0):
        return type("CompletedProcess", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode})()

    def test_dry_run_appends_dry_run_flag_and_relays_output(self):
        with patch.object(tool.subprocess, "run", return_value=self._completed(stdout="would tar 1 file")) as run, \
             patch("builtins.print") as mock_print:
            tool.run_archive("/nb/*_final", "b", "auto", 730, dry_run=True)
        self.assertIn("--dry-run", run.call_args[0][0])
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("would tar 1 file", printed)

    def test_real_run_omits_dry_run_flag(self):
        with patch.object(tool.subprocess, "run", return_value=self._completed()) as run:
            tool.run_archive("/nb/*_final", "b", "auto", 730, dry_run=False)
        self.assertNotIn("--dry-run", run.call_args[0][0])

    def test_missing_freezer_backup_runner_binary_raises(self):
        # Deliberately not caught - a missing freezer_backup_runner should fail
        # loudly, not silently skip the preview/kickoff run.
        with patch.object(tool.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                tool.run_archive("/nb/*_final", "b", "auto", 730, dry_run=True)

    def test_real_run_nonzero_exit_does_not_raise(self):
        with patch.object(tool.subprocess, "run", return_value=self._completed(returncode=1)), \
             patch("builtins.print"):
            tool.run_archive("/nb/*_final", "b", "auto", 730, dry_run=False)  # must not raise

    def test_dry_run_nonzero_exit_does_not_warn(self):
        # the retry-on-schedule note only makes sense for a real run.
        with patch.object(tool.subprocess, "run", return_value=self._completed(returncode=1)), \
             patch("builtins.print") as mock_print:
            tool.run_archive("/nb/*_final", "b", "auto", 730, dry_run=True)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertNotIn("retry", printed)


    def test_never_passes_a_destructive_flag(self):
        with patch.object(tool.subprocess, "run", return_value=self._completed()) as run:
            tool.run_archive("/nb/*_final", "b", "auto", 730, dry_run=False)
        self.assertNotIn("--delete-expired", run.call_args[0][0])
        self.assertNotIn("--overwrite", run.call_args[0][0])

    def test_log_level_is_passed_as_log_level(self):
        with patch.object(tool.subprocess, "run", return_value=self._completed()) as run:
            tool.run_archive("/nb/*_final", "b", "auto", 730, dry_run=False, log_level="DEBUG")
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("--log-level") + 1], "DEBUG")


class ValidateTests(unittest.TestCase):
    def _completed(self, stdout="", stderr="", returncode=0):
        return type("CompletedProcess", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode})()

    def test_missing_base_dir_raises(self):
        with self.assertRaises(ValueError):
            tool.validate("/definitely/does/not/exist/*_final", "some-bucket")

    def test_valid_writable_base_dir_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tool.subprocess, "run", return_value=self._completed()):
                tool.validate(str(Path(tmp) / "*_final"), "some-bucket")  # should return normally

    def test_freezer_access_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tool.subprocess, "run", return_value=self._completed(
                stderr="ERROR: S3 error: 404 (NoSuchBucket)", returncode=12,
            )):
                with self.assertRaises(ValueError):
                    tool.validate(str(Path(tmp) / "*_final"), "some-bucket")

    def test_freezer_not_configured_raises_with_clear_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tool.subprocess, "run", return_value=self._completed(returncode=78)):
                with self.assertRaises(ValueError) as cm:
                    tool.validate(str(Path(tmp) / "*_final"), "some-bucket")
        self.assertIn("not configured", str(cm.exception))

    def test_s3cmd_not_installed_raises_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tool.subprocess, "run", side_effect=FileNotFoundError):
                with self.assertRaises(ValueError):
                    tool.validate(str(Path(tmp) / "*_final"), "some-bucket")

    def test_embedded_single_quote_raises(self):
        # build_entry() wraps pattern in single quotes for the scrontab
        # line - an embedded "'" would break that quoting when scrontab
        # actually runs the entry.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                tool.validate(str(Path(tmp) / "o'brien_final"), "some-bucket")

    def test_group_writable_dir_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o775)
            with patch.object(tool.subprocess, "run", return_value=self._completed()), \
                 patch("sys.stderr") as mock_stderr:
                tool.validate(str(Path(tmp) / "*_final"), "some-bucket")
            mock_stderr.write.assert_not_called()

    def test_non_group_writable_dir_warns_in_shared_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            with patch.object(tool.subprocess, "run", return_value=self._completed()), \
                 patch.object(tool, "SHARED_ROOTS", (os.path.realpath(tmp),)), \
                 patch("sys.stderr") as mock_stderr:
                tool.validate(str(Path(tmp) / "*_final"), "some-bucket")
            self.assertTrue(mock_stderr.write.called)

    def test_non_group_writable_dir_outside_shared_space_does_not_warn(self):
        # e.g. /home - not group-writable by design.
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            with patch.object(tool.subprocess, "run", return_value=self._completed()), \
                 patch.object(tool, "SHARED_ROOTS", ("/nonexistent-shared-root",)), \
                 patch("sys.stderr") as mock_stderr:
                tool.validate(str(Path(tmp) / "*_final"), "some-bucket")
            mock_stderr.write.assert_not_called()


class ParseArgsTests(unittest.TestCase):
    def test_add_missing_pattern_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_add_args(["--bucket", "b"])

    def test_add_missing_bucket_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_add_args(["--pattern", "/nb/*_final"])

    def test_add_relative_pattern_is_resolved_against_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                args = tool.parse_add_args(["--pattern", "sub/*_final", "--bucket", "b"])
            finally:
                os.chdir(original_cwd)
            self.assertEqual(args.pattern, str(Path(tmp) / "sub" / "*_final"))

    def test_add_absolute_pattern_is_unchanged(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertEqual(args.pattern, "/nb/*_final")

    def test_add_retention_days_defaults_to_none(self):
        # None signals build_entry() to omit --retention-days, letting
        # freezer_backup_runner apply its own default.
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertIsNone(args.retention_days)

    def test_add_retention_days_short_flag_is_parsed(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b", "-r", "365"])
        self.assertEqual(args.retention_days, 365)

    def test_add_retention_days_zero_is_accepted(self):
        args = tool.parse_add_args(
            ["--pattern", "/nb/*_final", "--bucket", "b", "--retention-days", "0"]
        )
        self.assertEqual(args.retention_days, 0)

    def test_add_retention_days_non_integer_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_add_args(
                ["--pattern", "/nb/*_final", "--bucket", "b", "--retention-days", "forever"]
            )

    def test_add_retention_days_negative_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_add_args(
                ["--pattern", "/nb/*_final", "--bucket", "b", "--retention-days", "-1"]
            )

    def test_add_log_level_defaults_to_none(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertIsNone(args.log_level)

    def test_add_log_level_is_upper_cased(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b", "-l", "debug"])
        self.assertEqual(args.log_level, "DEBUG")

    def test_add_log_level_invalid_exits(self):
        with self.assertRaises(SystemExit), patch("sys.stderr", new=io.StringIO()):
            tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b", "-l", "TRACE"])

    def test_add_log_level_round_trips_through_scrontab(self):
        line = tool.build_entry("0 2 * * *", "/nb/*_final", "b", log_level="DEBUG")
        self.assertIn("--log-level DEBUG", line)
        self.assertNotIn("--mail-user", line)
        with patch.object(tool, "read_scrontab", return_value=line):
            self.assertEqual(tool.list_entries()[0]["log_level"], "DEBUG")

    def test_add_mail_user_defaults_to_none(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertIsNone(args.mail_user)

    def test_add_mail_user_long_flag_is_parsed(self):
        args = tool.parse_add_args(
            ["--pattern", "/nb/*_final", "--bucket", "b", "--mail-user", "a@example.com"]
        )
        self.assertEqual(args.mail_user, "a@example.com")

    def test_add_mail_user_short_flag_is_parsed(self):
        args = tool.parse_add_args(
            ["-p", "/nb/*_final", "-b", "b", "-m", "a@example.com"]
        )
        self.assertEqual(args.mail_user, "a@example.com")

    def test_add_mail_user_with_whitespace_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_add_args(
                ["--pattern", "/nb/*_final", "--bucket", "b", "--mail-user", "not an email"]
            )

    def test_add_no_run_defaults_false(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertFalse(args.no_run)

    def test_add_no_run_flag_is_parsed(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b", "--no-run"])
        self.assertTrue(args.no_run)

    def test_add_yes_defaults_false(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertFalse(args.yes)

    def test_add_yes_long_flag_is_parsed(self):
        args = tool.parse_add_args(["--pattern", "/nb/*_final", "--bucket", "b", "--yes"])
        self.assertTrue(args.yes)

    def test_add_yes_short_flag_is_parsed(self):
        args = tool.parse_add_args(["-p", "/nb/*_final", "-b", "b", "-y"])
        self.assertTrue(args.yes)

    def test_add_stray_positional_between_flags_exits_instead_of_swallowing_rest(self):
        # getopt stops parsing options at the first non-option argument, so
        # a stray word here would otherwise silently drop --bucket entirely.
        with self.assertRaises(SystemExit):
            tool.parse_add_args(["--pattern", "/nb/*_final", "stray", "--bucket", "b"])

    def test_remove_without_pattern_or_all_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_remove_args([])

    def test_remove_all_does_not_require_pattern(self):
        pattern, remove_all, _yes = tool.parse_remove_args(["--all"])
        self.assertTrue(remove_all)
        self.assertIsNone(pattern)

    def test_remove_relative_pattern_is_resolved_against_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                id_, _remove_all, _yes = tool.parse_remove_args(["--pattern", "sub/*_final"])
            finally:
                os.chdir(original_cwd)
            self.assertEqual(id_, tool.entry_id(str(Path(tmp) / "sub" / "*_final")))

    def test_remove_yes_defaults_false(self):
        _id, _remove_all, yes = tool.parse_remove_args(["--all"])
        self.assertFalse(yes)

    def test_remove_yes_flag_is_parsed(self):
        _id, _remove_all, yes = tool.parse_remove_args(["--all", "--yes"])
        self.assertTrue(yes)

    def test_status_does_not_require_pattern(self):
        pattern = tool.parse_status_args([])
        self.assertIsNone(pattern)

    def test_status_relative_pattern_is_resolved_against_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                id_ = tool.parse_status_args(["--pattern", "sub/*_final"])
            finally:
                os.chdir(original_cwd)
            self.assertEqual(id_, tool.entry_id(str(Path(tmp) / "sub" / "*_final")))


class CmdRemoveTests(unittest.TestCase):
    def test_remove_by_full_pattern(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write:
            result = tool.cmd_remove(["--pattern", "/nb/*_final", "--yes"])
        self.assertEqual(result, 0)
        self.assertNotIn("/nb/*_final", write.call_args[0][0])

    def test_remove_by_short_id(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        id_ = tool.entry_id("/nb/*_final")
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write:
            result = tool.cmd_remove(["--pattern", id_, "--yes"])
        self.assertEqual(result, 0)
        self.assertNotIn("/nb/*_final", write.call_args[0][0])

    def test_remove_nonexistent_id_reports_error_and_does_not_write(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            result = tool.cmd_remove(["--pattern", "abc123", "--yes"])
        self.assertEqual(result, 1)
        write.assert_not_called()

    def test_remove_all_ignores_pattern_and_removes_everything_managed(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write:
            result = tool.cmd_remove(["--all", "--yes"])
        self.assertEqual(result, 0)
        self.assertNotIn(tool.MARKER_PREFIX, write.call_args[0][0])

    def test_remove_without_yes_prompts_and_declining_does_not_write(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write, \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="n"):
            result = tool.cmd_remove(["--pattern", "/nb/*_final"])
        self.assertEqual(result, 1)
        write.assert_not_called()

    def test_remove_without_yes_prompts_and_confirming_writes(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write, \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="y"):
            result = tool.cmd_remove(["--pattern", "/nb/*_final"])
        self.assertEqual(result, 0)
        write.assert_called_once()

    def test_remove_non_interactive_without_yes_refuses(self):
        existing = tool.build_entry("0 2 * * *", "/nb/*_final", "b", "auto")
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write, \
             patch("sys.stdin.isatty", return_value=False), \
             patch("builtins.input") as mock_input:
            result = tool.cmd_remove(["--pattern", "/nb/*_final"])
        self.assertEqual(result, 1)
        write.assert_not_called()
        mock_input.assert_not_called()

    def test_remove_all_without_yes_lists_every_entry_before_prompting(self):
        entry_a = tool.build_entry("0 2 * * *", "/nb/a/*_final", "b", "auto")
        entry_b = tool.build_entry("0 2 * * *", "/nb/b/*_final", "b", "auto")
        with patch.object(tool, "read_scrontab", return_value=entry_a + entry_b), \
             patch.object(tool, "write_scrontab") as write, \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="n"), \
             patch("builtins.print") as mock_print:
            result = tool.cmd_remove(["--all"])
        self.assertEqual(result, 1)
        write.assert_not_called()
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("/nb/a/*_final", printed)
        self.assertIn("/nb/b/*_final", printed)

    def test_remove_all_with_no_entries_does_not_prompt(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write, \
             patch("builtins.input") as mock_input:
            result = tool.cmd_remove(["--all"])
        self.assertEqual(result, 0)
        write.assert_not_called()
        mock_input.assert_not_called()

class CmdStatusTests(unittest.TestCase):
    def test_status_filters_by_short_id(self):
        entry_a = tool.build_entry("0 2 * * *", "/nb/a/*_final", "b", "auto")
        entry_b = tool.build_entry("0 2 * * *", "/nb/b/*_final", "b", "auto")
        with patch.object(tool, "read_scrontab", return_value=entry_a + entry_b), \
             patch("builtins.print") as mock_print:
            result = tool.cmd_status(["--pattern", tool.entry_id("/nb/a/*_final")])
        self.assertEqual(result, 0)
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list)
        self.assertIn("/nb/a/*_final", printed)
        self.assertNotIn("/nb/b/*_final", printed)

    def test_status_nonexistent_id_reports_error(self):
        with patch.object(tool, "read_scrontab", return_value=""):
            result = tool.cmd_status(["--pattern", "abc123"])
        self.assertEqual(result, 1)


class ManualCommandTests(unittest.TestCase):
    def test_carries_the_entrys_own_retention_and_compress(self):
        # A hand run with the runner's default retention could delete by the
        # wrong period, so the entry's own settings must be in the command.
        entry = {"pattern": "/nb/my data/*_final", "bucket": "b", "compress": "never", "retention_days": "365"}
        self.assertEqual(
            tool.manual_command(entry, "--delete-expired"),
            "freezer_backup_runner --pattern '/nb/my data/*_final' --bucket b --compress never "
            "--retention-days 365 --delete-expired",
        )

    def test_omits_unset_settings(self):
        entry = {"pattern": "/nb/*_final", "bucket": "b", "compress": None, "retention_days": None}
        self.assertEqual(
            tool.manual_command(entry, "--overwrite"),
            "freezer_backup_runner --pattern '/nb/*_final' --bucket b --overwrite",
        )


class ScheduledRunsTests(unittest.TestCase):
    def _completed(self, stdout="", returncode=0):
        return type("CompletedProcess", (), {"stdout": stdout, "stderr": "", "returncode": returncode})()

    def test_parses_squeue_output_by_job_name(self):
        stdout = (
            "freezer_backup-abc123|PENDING|2026-09-24 02:00\n"
            "freezer_backup-def456|RUNNING|2026-09-23 02:00\n"
            "batch.slurm|RUNNING|2026-09-23 12:07\n"
        )
        with patch.object(tool.subprocess, "run", return_value=self._completed(stdout)) as run:
            runs = REAL_SCHEDULED_RUNS()
        self.assertEqual(runs["freezer_backup-abc123"], ("PENDING", "2026-09-24 02:00"))
        self.assertEqual(runs["freezer_backup-def456"], ("RUNNING", "2026-09-23 02:00"))
        # a fixed time format, whatever the user's own SLURM_TIME_FORMAT is
        self.assertEqual(run.call_args.kwargs["env"]["SLURM_TIME_FORMAT"], "%Y-%m-%d %H:%M")

    def test_squeue_missing_returns_none(self):
        with patch.object(tool.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(REAL_SCHEDULED_RUNS())

    def test_squeue_failure_returns_none(self):
        with patch.object(tool.subprocess, "run", return_value=self._completed(returncode=1)):
            self.assertIsNone(REAL_SCHEDULED_RUNS())


class NextRunTests(unittest.TestCase):
    ENTRY = {"id": "abc123"}

    def test_pending_job_shows_its_start_time(self):
        runs = {"freezer_backup-abc123": ("PENDING", "2026-09-24 02:00")}
        self.assertEqual(tool.format_next_run(self.ENTRY, runs), "2026-09-24 02:00")

    def test_running_job_says_so(self):
        runs = {"freezer_backup-abc123": ("RUNNING", "2026-09-23 02:00")}
        self.assertEqual(tool.format_next_run(self.ENTRY, runs), "running now (started 2026-09-23 02:00)")

    def test_no_job_is_not_scheduled(self):
        self.assertIn("not scheduled", tool.format_next_run(self.ENTRY, {}))

    def test_squeue_unavailable_is_unknown(self):
        self.assertIn("unknown", tool.format_next_run(self.ENTRY, None))

    def test_format_status_shows_next_run_and_no_touch_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {"id": "abc123", "pattern": str(Path(tmp) / "*_final"),
                     "bucket": "b", "schedule": "0 2 * * *", "retention_days": "0"}
            output = tool.format_status(entry, runs={"freezer_backup-abc123": ("PENDING", "2026-09-24 02:00")})
        self.assertIn("next run:    2026-09-24 02:00", output)
        self.assertNotIn("touch", output)

    def test_cmd_status_calls_squeue_once_for_every_entry(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a/*_final", "b", retention_days=0)
        table = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=table), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/b/*_final", "b", retention_days=0)
        table = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=table), \
             patch.object(tool, "scheduled_runs", return_value={}) as runs, \
             patch("builtins.print"):
            tool.cmd_status([])
        runs.assert_called_once()

    def test_entries_written_by_add_are_found_by_their_job_name(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b")
        with patch.object(tool, "read_scrontab", return_value=write.call_args[0][0]):
            entry = tool.list_entries()[0]
        runs = {tool.job_name_for(entry["id"]): ("PENDING", "2026-09-24 02:00")}
        self.assertEqual(tool.format_next_run(entry, runs), "2026-09-24 02:00")


class CleanerListTests(unittest.TestCase):
    """`status` counts pending files on nobackup's auto-delete list, when it can read the list."""

    def _setup(self, tmp):
        root = Path(tmp) / "nobackup"
        base = root / "proj01" / "data"
        (base / "a_final" / "sub").mkdir(parents=True)
        (base / "b_wip").mkdir()
        list_dir = Path(tmp) / "lists"
        list_dir.mkdir()
        return root, base, list_dir

    def _write_list(self, list_dir, paths, project="proj01"):
        with gzip.open(list_dir / f"{project}.gz", "wt") as fh:
            fh.writelines(f"{p}\n" for p in paths)

    def _count(self, root, base, list_dir, metadata_records=()):
        metadata_path = base / ".freezer" / "metadata.jsonl"
        if metadata_records:
            metadata_path.parent.mkdir()
            metadata_path.write_text("".join(json.dumps(r) + "\n" for r in metadata_records))
        with patch.object(tool, "NOBACKUP_ROOT", str(root)), \
             patch.object(tool, "AUTOCLEANER_LIST_DIR", str(list_dir)):
            return tool.pending_on_cleaner_list(str(base / "*_final"), metadata_path)

    def test_counts_only_pending_existing_files_in_matching_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base, list_dir = self._setup(tmp)
            for rel in ("a_final/pending.txt", "a_final/sub/deep.txt", "a_final/archived.txt", "b_wip/x.txt"):
                (base / rel).write_text("x")
            self._write_list(list_dir, [
                base / "a_final/pending.txt",       # counts
                base / "a_final/sub/deep.txt",      # counts
                base / "a_final/archived.txt",      # already archived
                base / "a_final/gone.txt",          # deleted since the list was made
                base / "b_wip/x.txt",               # folder doesn't match
                base / ".freezer/metadata.jsonl",   # tool's own state
                root / "proj01/other/a_final/y",    # a different base dir
            ])
            count = self._count(root, base, list_dir, [
                {"folder": "a_final", "file": "archived.txt", "tar_name": "t"},
            ])
        self.assertEqual(count, 2)

    def test_zero_when_nothing_pending_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base, list_dir = self._setup(tmp)
            self._write_list(list_dir, [])
            self.assertEqual(self._count(root, base, list_dir), 0)

    def test_none_when_list_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base, list_dir = self._setup(tmp)  # no list file written
            self.assertIsNone(self._count(root, base, list_dir))

    def test_none_outside_nobackup(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            root, _base, list_dir = self._setup(tmp)
            with patch.object(tool, "NOBACKUP_ROOT", str(root)), \
                 patch.object(tool, "AUTOCLEANER_LIST_DIR", str(list_dir)):
                self.assertIsNone(tool.pending_on_cleaner_list(f"{elsewhere}/*_final", Path(elsewhere) / "m"))

    def test_format_status_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {"id": "abc123", "pattern": str(Path(tmp) / "*_final"),
                     "bucket": "b", "schedule": "0 2 * * *", "retention_days": "0"}
            with patch.object(tool, "pending_on_cleaner_list", return_value=3):
                stuck = tool.format_status(entry, runs={})
            with patch.object(tool, "pending_on_cleaner_list", return_value=0):
                fine = tool.format_status(entry, runs={})
            with patch.object(tool, "pending_on_cleaner_list", return_value=None):
                unknown = tool.format_status(entry, runs={})
        self.assertIn("cleaner:     3 pending file(s) on nobackup's auto-delete list", stuck)
        self.assertIn("cleaner:     no pending files", fine)
        self.assertNotIn("cleaner:", unknown)


class MainDispatchTests(unittest.TestCase):
    def test_unknown_subcommand_returns_nonzero(self):
        self.assertNotEqual(tool.main(["bogus"]), 0)

    def test_no_args_prints_usage_and_succeeds(self):
        self.assertEqual(tool.main([]), 0)


if __name__ == "__main__":
    unittest.main()


class SlurmAccountsTests(unittest.TestCase):
    ASSOC = (
        "ClusterName=hpc Account=root UserName= Partition= DefAssoc=No\n"
        "ClusterName=hpc Account=proj00001 UserName=someone(1) Partition= DefAssoc=No\n"
        "ClusterName=hpc Account=proj00002 UserName=someone(1) Partition= DefAssoc=Yes\n"
        "ClusterName=hpc Account=proj00003 UserName=other(2) Partition= DefAssoc=Yes\n"
    )

    def _run(self, stdout="", returncode=0):
        completed = type("CompletedProcess", (), {"stdout": stdout, "returncode": returncode})()
        with patch.dict(os.environ, {"USER": "someone"}), \
             patch.object(tool.subprocess, "run", return_value=completed):
            return REAL_SLURM_ACCOUNTS()

    def test_lists_own_accounts_and_default(self):
        self.assertEqual(self._run(self.ASSOC), (["proj00001", "proj00002"], "proj00002"))

    def test_no_default(self):
        self.assertEqual(self._run(self.ASSOC.replace("DefAssoc=Yes", "DefAssoc=No")),
                         (["proj00001", "proj00002"], None))

    def test_scontrol_failure_is_none(self):
        self.assertIsNone(self._run(returncode=1))

    def test_missing_scontrol_is_none(self):
        with patch.object(tool.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(REAL_SLURM_ACCOUNTS())


class AccountTests(unittest.TestCase):
    NO_DEFAULT = (["proj00001", "proj00002"], None)

    def _add(self, *flags, table="", accounts=NO_DEFAULT, tty=False, answer=""):
        """cmd_add's return code and written table ("" if nothing written)."""
        with patch.object(tool, "validate"), \
             patch.object(tool, "read_scrontab", return_value=table), \
             patch.object(tool, "write_scrontab") as write, \
             patch.object(tool, "run_archive"), \
             patch.object(tool, "slurm_accounts", return_value=accounts), \
             patch.object(tool.sys.stdin, "isatty", return_value=tty), \
             patch("builtins.input", return_value=answer), \
             patch("sys.stdout", new_callable=io.StringIO), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = tool.cmd_add(["--pattern", "/nb/*_final", "--bucket", "b", "--yes", *flags])
        return rc, write.call_args[0][0] if write.called else ""

    def _directive(self, account=None):
        return tool.directive_for(tool.entry_id("/nb/*_final"), account=account)

    def test_directive_carries_account(self):
        self.assertEqual(tool.directive_for("abc123", "a@example.com", "proj00001"),
                         "#SCRON --job-name=freezer_backup-abc123 --account=proj00001 "
                         "--mail-type=FAIL --mail-user=a@example.com\n")

    def test_list_entries_parses_account_back(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/*_final", "b", mail_user="a@example.com",
                                     account="proj00001")
        with patch.object(tool, "read_scrontab", return_value=write.call_args[0][0]):
            entry, = tool.list_entries()
        self.assertEqual((entry["account"], entry["mail_user"]), ("proj00001", "a@example.com"))

    def test_account_flag_is_written(self):
        rc, written = self._add("--account", "proj00002")
        self.assertEqual(rc, 0)
        self.assertIn(self._directive("proj00002"), written)

    def test_short_flag(self):
        self.assertIn(self._directive("proj00002"), self._add("-A", "proj00002")[1])

    def test_unknown_account_is_refused(self):
        self.assertEqual(self._add("--account", "nope"), (1, ""))

    def test_default_account_writes_none(self):
        rc, written = self._add(accounts=(["proj00001"], "proj00001"))
        self.assertIn(self._directive(), written)

    def test_no_default_non_interactive_refuses(self):
        self.assertEqual(self._add(), (1, ""))

    def test_no_default_prompts_by_number(self):
        self.assertIn(self._directive("proj00002"), self._add(tty=True, answer="2")[1])

    def test_no_default_prompts_by_name(self):
        self.assertIn(self._directive("proj00001"), self._add(tty=True, answer="proj00001")[1])

    def test_no_default_bad_answer_refuses(self):
        self.assertEqual(self._add(tty=True, answer="9"), (1, ""))

    def test_no_default_rerun_keeps_existing_account(self):
        _, first = self._add("--account", "proj00002")
        rc, written = self._add(table=first)
        self.assertEqual(rc, 0)
        self.assertIn(self._directive("proj00002"), written)

    def test_unknown_accounts_leaves_it_to_scrontab(self):
        rc, written = self._add("--account", "whatever", accounts=None)
        self.assertIn(self._directive("whatever"), written)
