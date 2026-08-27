"""Tests for archive_tool. Skeleton only - see PLAN.md Phase 6."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import archive_tool as tool  # noqa: E402


class EntryIdentityTests(unittest.TestCase):
    def test_same_folder_and_pattern_gives_same_id(self):
        self.assertEqual(tool.entry_id("/nb", "*_final"), tool.entry_id("/nb", "*_final"))

    def test_different_pattern_gives_different_id(self):
        self.assertNotEqual(tool.entry_id("/nb", "*_final"), tool.entry_id("/nb", "*_done"))

    def test_different_folder_gives_different_id(self):
        self.assertNotEqual(tool.entry_id("/nb/a", "*_final"), tool.entry_id("/nb/b", "*_final"))


class ScrontabEntryTests(unittest.TestCase):
    @patch.object(tool, "read_scrontab", return_value="")
    @patch.object(tool, "write_scrontab")
    def test_add_adds_one_marked_line(self, write, _read):
        tool.add_or_update_entry("0 2 * * *", "/some/nobackup", "mybucket", "*_final", "auto")
        written = write.call_args[0][0]
        self.assertEqual(written.count(tool.MARKER_PREFIX), 1)
        self.assertIn("mybucket", written)

    def test_pattern_is_quoted_against_shell_glob_expansion(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb", "b", "*_final", "auto")
        written = write.call_args[0][0]
        self.assertIn("'*_final'", written)

    def test_rerun_same_folder_and_pattern_replaces_not_duplicates(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb", "old", "*_final", "auto")
        existing = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 3 * * *", "/nb", "new", "*_final", "auto")
        written = write2.call_args[0][0]

        self.assertEqual(written.count(tool.MARKER_PREFIX), 1)
        self.assertIn("--bucket new", written)
        self.assertNotIn("--bucket old", written)

    def test_different_folder_or_pattern_adds_second_entry(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a", "b", "*_final", "auto")
        after_first = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=after_first), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 2 * * *", "/nb/b", "b", "*_final", "auto")
        written = write2.call_args[0][0]

        self.assertEqual(written.count(tool.MARKER_PREFIX), 2)
        self.assertIn("/nb/a", written)
        self.assertIn("/nb/b", written)

    def test_add_preserves_unrelated_entries(self):
        existing = "0 1 * * * /some/other/job\n"
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb", "b", "*_final", "auto")
        written = write.call_args[0][0]
        self.assertIn("/some/other/job", written)

    def test_remove_entry_removes_only_matching_folder_and_pattern(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb/a", "b", "*_final", "auto")
        after_a = write.call_args[0][0]
        with patch.object(tool, "read_scrontab", return_value=after_a), \
             patch.object(tool, "write_scrontab") as write2:
            tool.add_or_update_entry("0 2 * * *", "/nb/b", "b", "*_final", "auto")
        after_both = write2.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=after_both), \
             patch.object(tool, "write_scrontab") as write3:
            tool.remove_entry("/nb/a", "*_final")
        written = write3.call_args[0][0]

        self.assertNotIn("/nb/a", written)
        self.assertIn("/nb/b", written)

    def test_remove_all_entries_removes_everything_managed_but_not_unrelated(self):
        existing = (
            "0 1 * * * /some/other/job\n"
            f"0 2 * * * runner_archive --folder /nb/a --bucket b --pattern '*_final' --compress auto {tool.marker_for(tool.entry_id('/nb/a', '*_final'))}\n"
            f"0 2 * * * runner_archive --folder /nb/b --bucket b --pattern '*_final' --compress auto {tool.marker_for(tool.entry_id('/nb/b', '*_final'))}\n"
        )
        with patch.object(tool, "read_scrontab", return_value=existing), \
             patch.object(tool, "write_scrontab") as write:
            tool.remove_all_entries()
        written = write.call_args[0][0]
        self.assertNotIn(tool.MARKER_PREFIX, written)
        self.assertIn("/some/other/job", written)


class ListAndStatusTests(unittest.TestCase):
    def test_list_entries_parses_folder_bucket_pattern_schedule(self):
        with patch.object(tool, "read_scrontab", return_value=""), \
             patch.object(tool, "write_scrontab") as write:
            tool.add_or_update_entry("0 2 * * *", "/nb", "mybucket", "*_final", "always")
        existing = write.call_args[0][0]

        with patch.object(tool, "read_scrontab", return_value=existing):
            entries = tool.list_entries()

        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["folder"], "/nb")
        self.assertEqual(e["bucket"], "mybucket")
        self.assertEqual(e["pattern"], "*_final")
        self.assertEqual(e["compress"], "always")
        self.assertEqual(e["schedule"], "0 2 * * *")

    def test_count_metadata_entries_counts_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            path.write_text('{"folder": "x", "file": "a"}\n{"folder": "x", "file": "b"}\n')
            self.assertEqual(tool.count_metadata_entries(path), 2)

    def test_count_metadata_entries_zero_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does_not_exist.jsonl"
            self.assertEqual(tool.count_metadata_entries(path), 0)

    def test_last_activity_never_when_no_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.log"
            self.assertEqual(tool.last_activity(path), "never")


class ValidateTests(unittest.TestCase):
    def test_missing_folder_raises(self):
        with self.assertRaises(ValueError):
            tool.validate("/definitely/does/not/exist", "some-bucket")

    def test_valid_writable_folder_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool.validate(tmp, "some-bucket")  # should return normally


class ParseArgsTests(unittest.TestCase):
    def test_add_missing_folder_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_add_args(["--bucket", "b"])

    def test_add_missing_bucket_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_add_args(["--folder", "/nb"])

    def test_remove_without_folder_or_all_exits(self):
        with self.assertRaises(SystemExit):
            tool.parse_remove_args([])

    def test_remove_all_does_not_require_folder(self):
        folder, _pattern, remove_all = tool.parse_remove_args(["--all"])
        self.assertTrue(remove_all)
        self.assertIsNone(folder)

    def test_status_does_not_require_folder(self):
        folder, _pattern = tool.parse_status_args([])
        self.assertIsNone(folder)


class MainDispatchTests(unittest.TestCase):
    def test_unknown_subcommand_returns_nonzero(self):
        self.assertNotEqual(tool.main(["bogus"]), 0)

    def test_planned_subcommand_returns_nonzero_not_a_crash(self):
        self.assertNotEqual(tool.main(["enable"]), 0)

    def test_no_args_prints_usage_and_succeeds(self):
        self.assertEqual(tool.main([]), 0)


if __name__ == "__main__":
    unittest.main()
