"""
Tests for freezer_backup_runner.

Uses tempfile for a fake nobackup and unittest.mock to stub out
s3cmd/tar, so these run without real Freezer access.
"""

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# .freezer_backup_runner.py is hidden (leading dot), so it can't be imported by name
_spec = importlib.util.spec_from_file_location("freezer_backup_runner", Path(__file__).resolve().parent.parent / ".freezer_backup_runner.py")
archive = sys.modules["freezer_backup_runner"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(archive)


class MetadataTests(unittest.TestCase):
    def test_load_metadata_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            self.assertEqual(archive.load_metadata(path), {})

    def test_append_metadata_persists_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            record = {"folder": "x_final", "file": "a.txt", "tar_name": "x-1"}
            archive.append_metadata(path, [record])
            self.assertEqual(archive.load_metadata(path), {("x_final", "a.txt"): record})

    def test_append_metadata_with_no_records_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(path, [])
            self.assertFalse(path.exists())

    def test_load_metadata_skips_corrupt_trailing_line(self):
        # SPEC Open Questions: a crash mid-append can leave a partial line.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            good = '{"folder": "x_final", "file": "a.txt"}\n'
            path.write_text(good + '{"folder": "x_final", "fi')
            result = archive.load_metadata(path)
            self.assertIn(("x_final", "a.txt"), result)


class DiffTests(unittest.TestCase):
    def test_new_file_not_in_metadata_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            (folder / "a.txt").write_text("data")
            new_files = list(archive.unarchived_files(folder, archived={}))
            self.assertEqual([p.name for p in new_files], ["a.txt"])

    def test_already_archived_file_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            (folder / "a.txt").write_text("data")
            archived = {("sample_final", "a.txt"): {"folder": "sample_final", "file": "a.txt"}}
            self.assertEqual(list(archive.unarchived_files(folder, archived)), [])


class DiscoverFoldersTests(unittest.TestCase):
    def test_pattern_matches_final_suffix_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample_final").mkdir()
            (root / "sample_wip").mkdir()
            found = archive.discover_folders(root, "*_final")
            self.assertEqual([p.name for p in found], ["sample_final"])

    def test_custom_pattern_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample_done").mkdir()
            (root / "sample_final").mkdir()
            found = archive.discover_folders(root, "*_done")
            self.assertEqual([p.name for p in found], ["sample_done"])

    def test_only_top_level_directories_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample_final").mkdir()
            (root / "sample_final.txt").write_text("not a folder")
            found = archive.discover_folders(root, "*_final")
            self.assertEqual([p.name for p in found], ["sample_final"])

    def test_state_dir_excluded_even_when_pattern_would_match_it(self):
        # pathlib's glob("*"), unlike a shell glob, matches dotdirs - a
        # wildcard-ish pattern would otherwise sweep up this tool's own
        # metadata/lock/log directory and try to archive it as data.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / archive.STATE_DIR_NAME).mkdir()
            (root / "data_final").mkdir()
            found = archive.discover_folders(root, "*")
            self.assertEqual([p.name for p in found], ["data_final"])

    def test_state_dir_excluded_with_a_pattern_matching_its_literal_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / archive.STATE_DIR_NAME).mkdir()
            found = archive.discover_folders(root, archive.STATE_DIR_NAME)
            self.assertEqual(found, [])


class SplitPatternTests(unittest.TestCase):
    def test_splits_absolute_base_dir_and_glob(self):
        base_dir, glob_expr = archive.split_pattern("/nesi/nobackup/uoa03387/*_final")
        self.assertEqual(base_dir, Path("/nesi/nobackup/uoa03387"))
        self.assertEqual(glob_expr, "*_final")

    def test_relative_pattern_splits_into_relative_base_dir(self):
        base_dir, glob_expr = archive.split_pattern("sub/*_final")
        self.assertEqual(base_dir, Path("sub"))
        self.assertEqual(glob_expr, "*_final")

    def test_bare_pattern_splits_into_cwd_relative_base_dir(self):
        base_dir, glob_expr = archive.split_pattern("*_final")
        self.assertEqual(base_dir, Path("."))
        self.assertEqual(glob_expr, "*_final")


class BucketUriTests(unittest.TestCase):
    def test_bucket_without_prefix(self):
        self.assertEqual(archive.bucket_uri("mybucket"), "s3://mybucket/")

    def test_bucket_with_prefix_is_not_doubled(self):
        self.assertEqual(archive.bucket_uri("s3://mybucket"), "s3://mybucket/")

    def test_bucket_with_uppercase_prefix(self):
        self.assertEqual(archive.bucket_uri("S3://mybucket"), "s3://mybucket/")

    def test_with_key(self):
        self.assertEqual(archive.bucket_uri("s3://mybucket", "foo.tar"), "s3://mybucket/foo.tar")

    def test_list_bucket_objects_accepts_prefixed_bucket(self):
        stdout = "2026-08-01 12:00  1234  s3://mybucket/sample_final-20260801.tar.gz\n"
        completed = type("CompletedProcess", (), {"stdout": stdout, "returncode": 0})()
        with patch.object(archive.subprocess, "run", return_value=completed) as run:
            objects = archive.list_bucket_objects("s3://mybucket")
        self.assertIn("sample_final-20260801.tar.gz", objects)
        self.assertEqual(run.call_args[0][0], ["s3cmd", "ls", "-l", "-H", "s3://mybucket/"])


class RunS3cmdTests(unittest.TestCase):
    def test_success_returns_completed_process(self):
        sentinel = object()
        with patch.object(archive.subprocess, "run", return_value=sentinel) as run:
            result = archive.run_s3cmd(["s3cmd", "ls"])
        self.assertIs(result, sentinel)
        self.assertTrue(run.call_args[1].get("check"))

    def test_not_configured_exit_code_warns_and_returns_none(self):
        error = archive.subprocess.CalledProcessError(78, ["s3cmd"])
        with patch.object(archive.subprocess, "run", side_effect=error):
            with self.assertLogs(archive.log, level="WARNING") as cm:
                result = archive.run_s3cmd(["s3cmd", "ls"])
        self.assertIsNone(result)
        self.assertTrue(any("s3cmd --configure" in msg for msg in cm.output))

    def test_not_configured_logs_at_error_not_warning(self):
        # ERROR, not WARNING - see run_s3cmd()'s comment: nothing this run
        # does its actual job without s3cmd configured.
        error = archive.subprocess.CalledProcessError(78, ["s3cmd"])
        with patch.object(archive.subprocess, "run", side_effect=error):
            with self.assertLogs(archive.log, level="WARNING") as cm:
                archive.run_s3cmd(["s3cmd", "ls"])
        self.assertTrue(any(r.levelname == "ERROR" for r in cm.records))

    def test_other_exit_codes_still_raise(self):
        error = archive.subprocess.CalledProcessError(1, ["s3cmd"])
        with patch.object(archive.subprocess, "run", side_effect=error):
            with self.assertRaises(archive.subprocess.CalledProcessError):
                archive.run_s3cmd(["s3cmd", "ls"])


class CompressionDetectionTests(unittest.TestCase):
    def test_gzip_magic_bytes_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reads.fastq.gz"
            path.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 20)
            self.assertTrue(archive.is_probably_compressed(path))

    def test_plain_text_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("just some plain text, nothing compressed here")
            self.assertFalse(archive.is_probably_compressed(path))

    def test_should_compress_always_and_never_ignore_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.gz"
            path.write_bytes(b"\x1f\x8b\x08\x00")
            self.assertTrue(archive.should_compress([path], "always"))
            self.assertFalse(archive.should_compress([path], "never"))

    def test_auto_skips_compression_when_mostly_already_compressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            big_compressed = Path(tmp) / "big.bam"
            small_plain = Path(tmp) / "readme.txt"
            big_compressed.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 10_000)
            small_plain.write_text("tiny uncompressed file")
            self.assertFalse(archive.should_compress([big_compressed, small_plain], "auto"))

    def test_auto_compresses_when_mostly_uncompressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            big_plain = Path(tmp) / "big.txt"
            small_compressed = Path(tmp) / "tiny.gz"
            big_plain.write_text("x" * 10_000)
            small_compressed.write_bytes(b"\x1f\x8b\x08\x00")
            self.assertTrue(archive.should_compress([big_plain, small_compressed], "auto"))

    def test_auto_ignores_a_file_that_vanishes_mid_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            surviving = Path(tmp) / "big.txt"
            vanished = Path(tmp) / "gone.gz"
            surviving.write_text("x" * 10_000)
            vanished.write_bytes(b"\x1f\x8b\x08\x00")
            vanished.unlink()
            # Should reason only about `surviving` (uncompressed) rather
            # than raising on the missing file.
            self.assertTrue(archive.should_compress([surviving, vanished], "auto"))

    def test_auto_all_files_vanished_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            vanished = Path(tmp) / "gone.txt"
            vanished.write_text("x")
            vanished.unlink()
            self.assertFalse(archive.should_compress([vanished], "auto"))


class TarchiveTests(unittest.TestCase):
    def _completed(self, stdout=""):
        return type("CompletedProcess", (), {"stdout": stdout, "returncode": 0})()

    def test_no_new_files_returns_empty_list_without_calling_s3cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            with patch.object(archive.subprocess, "run") as run:
                result = archive.tarchive(folder, new_files=[], bucket="test-bucket")
            run.assert_not_called()
            self.assertEqual(result, [])

    def test_uploads_real_tar_and_returns_one_record_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            file_path = folder / "a.txt"
            file_path.write_text("data")

            def fake_run(args, **kwargs):
                if args[1] == "put":
                    self.assertIn("--preserve", args)
                    self.assertIn("--multipart-chunk-size-mb=15", args)
                    self.assertIn("--add-header=x-amz-meta-chunksize:15", args)
                    self.assertTrue(args[-1].startswith("s3://test-bucket/sample_final-"))
                    self.assertTrue(Path(args[-2]).is_file(), "put should upload a real tar file")
                    return self._completed()
                if args[1] == "info":
                    return self._completed("   MD5 sum:   abc123\n")
                raise AssertionError(f"unexpected s3cmd invocation: {args}")

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                result = archive.tarchive(folder, new_files=[file_path], bucket="test-bucket")

            self.assertEqual(len(result), 1)
            record = result[0]
            self.assertEqual(record["folder"], "sample_final")
            self.assertEqual(record["file"], "a.txt")
            self.assertTrue(record["tar_name"].startswith("sample_final-"))
            self.assertEqual(record["checksum"], "abc123")
            self.assertEqual(record["size"], file_path.stat().st_size)

    def test_unconfigured_s3cmd_on_put_warns_and_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            file_path = folder / "a.txt"
            file_path.write_text("data")
            error = archive.subprocess.CalledProcessError(78, ["s3cmd"])
            with patch.object(archive.subprocess, "run", side_effect=error):
                with self.assertLogs(archive.log, level="WARNING") as cm:
                    result = archive.tarchive(folder, new_files=[file_path], bucket="test-bucket")
            self.assertEqual(result, [])
            self.assertTrue(any("s3cmd --configure" in msg for msg in cm.output))

    def test_file_vanished_before_the_tar_step_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            surviving = folder / "a.txt"
            surviving.write_text("data")
            vanished = folder / "b.txt"
            vanished.write_text("data")
            vanished.unlink()  # simulates the file disappearing after unarchived_files() ran

            with patch.object(archive.subprocess, "run", return_value=self._completed()):
                with self.assertLogs(archive.log, level="INFO") as cm:
                    result = archive.tarchive(folder, new_files=[surviving, vanished], bucket="test-bucket")

            self.assertEqual([r["file"] for r in result], ["a.txt"])
            self.assertTrue(any("vanished" in msg for msg in cm.output))

    def test_all_files_vanished_returns_empty_list_without_calling_s3cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            vanished = folder / "a.txt"
            vanished.write_text("data")
            vanished.unlink()

            with patch.object(archive.subprocess, "run") as run:
                result = archive.tarchive(folder, new_files=[vanished], bucket="test-bucket")

            run.assert_not_called()
            self.assertEqual(result, [])

    def test_file_vanishing_during_the_tar_step_itself_is_also_skipped(self):
        # A narrower race than the previous test: the file still exists at
        # the upfront check, but is gone by the time tar.add() actually
        # reads it (defense in depth around the tar.add() call itself).
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            surviving = folder / "a.txt"
            surviving.write_text("data")
            racy = folder / "b.txt"
            racy.write_text("data")

            def lying_exists(self):
                return True  # pretend `racy` is still there for the upfront filter

            with patch.object(Path, "exists", lying_exists):
                racy.unlink()  # now genuinely gone before tar.add() gets to it
                with patch.object(archive.subprocess, "run", return_value=self._completed()):
                    with self.assertLogs(archive.log, level="INFO") as cm:
                        result = archive.tarchive(folder, new_files=[surviving, racy], bucket="test-bucket")

            self.assertEqual([r["file"] for r in result], ["a.txt"])
            self.assertTrue(any("vanished" in msg for msg in cm.output))

    def test_second_same_day_archive_gets_a_distinct_tar_name(self):
        # Resumability/"no duplicate tars": without this, a second
        # successful run against the same folder on the same day would
        # silently overwrite the first run's tar under an identical key,
        # even though metadata still claims the first run's files are
        # archived there.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            first_file = folder / "a.txt"
            first_file.write_text("data")
            second_file = folder / "b.txt"
            second_file.write_text("more data")

            with patch.object(archive.subprocess, "run", return_value=self._completed()):
                first_records = archive.tarchive(folder, new_files=[first_file], bucket="test-bucket")
            archived = {("sample_final", r["file"]): r for r in first_records}

            with patch.object(archive.subprocess, "run", return_value=self._completed()):
                second_records = archive.tarchive(
                    folder, new_files=[second_file], bucket="test-bucket", archived=archived
                )

            self.assertNotEqual(first_records[0]["tar_name"], second_records[0]["tar_name"])


class NextTarNameTests(unittest.TestCase):
    def test_first_archive_of_the_day_uses_the_plain_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            expected = f"sample_final-{archive.date.today():%Y%m%d}.tar"
            self.assertEqual(archive.next_tar_name(folder, {}, ".tar"), expected)

    def test_collision_is_suffixed_with_an_incrementing_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            today = f"{archive.date.today():%Y%m%d}"
            archived = {
                ("sample_final", "a.txt"): {"tar_name": f"sample_final-{today}.tar"},
            }
            self.assertEqual(archive.next_tar_name(folder, archived, ".tar"), f"sample_final-{today}-2.tar")

    def test_multiple_collisions_keep_incrementing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            today = f"{archive.date.today():%Y%m%d}"
            archived = {
                ("sample_final", "a.txt"): {"tar_name": f"sample_final-{today}.tar"},
                ("sample_final", "b.txt"): {"tar_name": f"sample_final-{today}-2.tar"},
            }
            self.assertEqual(archive.next_tar_name(folder, archived, ".tar"), f"sample_final-{today}-3.tar")

    def test_different_folder_names_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "other_final"
            today = f"{archive.date.today():%Y%m%d}"
            archived = {
                ("sample_final", "a.txt"): {"tar_name": f"sample_final-{today}.tar"},
            }
            self.assertEqual(archive.next_tar_name(folder, archived, ".tar"), f"other_final-{today}.tar")


class ConcurrencyGuardTests(unittest.TestCase):
    def test_second_lock_acquisition_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            fh1 = archive.acquire_lock(lock_path)
            self.assertIsNone(archive.acquire_lock(lock_path))
            fh1.close()

    def test_lock_released_after_close_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            fh1 = archive.acquire_lock(lock_path)
            fh1.close()
            fh2 = archive.acquire_lock(lock_path)
            fh2.close()

    def test_main_returns_zero_without_crashing_when_already_locked(self):
        # End-to-end: a second concurrent invocation (manual run overlapping
        # the scheduled one, or two independently-scheduled installs on the
        # same project - SPEC's own caveat on the concurrency guard) must
        # exit cleanly with a log line, not a traceback. Not a failure (no
        # mail): the other run is doing the work.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample_final").mkdir()
            state_dir = Path(tmp) / ".freezer"
            state_dir.mkdir()
            held = archive.acquire_lock(state_dir / "lock")
            try:
                with self.assertLogs(archive.log, level="INFO") as cm:
                    result = archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
                self.assertEqual(result, 0)
                self.assertTrue(any("lock already held" in msg for msg in cm.output))
            finally:
                held.close()


class LoggingSplitTests(unittest.TestCase):
    """archive.log always gets everything (DEBUG+); stdout/stderr only gets what --log-level actually asked for."""

    def test_log_file_captures_debug_even_at_quieter_display_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "archive.log"
            archive.setup_logging(log_path, "ERROR")
            archive.log.debug("a debug breadcrumb")
            self.assertIn("a debug breadcrumb", log_path.read_text())

    def test_log_file_captures_info_even_at_quieter_display_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "archive.log"
            archive.setup_logging(log_path, "WARNING")
            archive.log.info("a routine info line")
            self.assertIn("a routine info line", log_path.read_text())

    def test_stderr_handler_level_matches_the_requested_display_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive.setup_logging(Path(tmp) / "archive.log", "ERROR")
        stream_handler = next(h for h in archive.log.handlers if h.stream is sys.stderr)
        self.assertEqual(stream_handler.level, archive.logging.ERROR)

    def test_file_handler_level_is_always_debug_regardless_of_display_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive.setup_logging(Path(tmp) / "archive.log", "ERROR")
        file_handler = next(h for h in archive.log.handlers if isinstance(h, archive.logging.FileHandler))
        self.assertEqual(file_handler.level, archive.logging.DEBUG)


class MainExitCodeTests(unittest.TestCase):
    """main() fails (returns 1) iff anything was logged at WARNING or up - that's what triggers the failure mail."""

    def test_main_returns_zero_for_a_clean_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample_final").mkdir()
            result = archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
            self.assertEqual(result, 0)

    def test_main_returns_nonzero_when_a_warning_was_logged(self):
        # A drifted file with no --overwrite logs a WARNING and is left
        # as-is - someone needs to act on it, so the run counts as failed.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            state_dir = Path(tmp) / ".freezer"
            state_dir.mkdir()
            archive.append_metadata(state_dir / "metadata.jsonl", [{
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                "size": 4, "mtime": path.stat().st_mtime - 100, "checksum": "abc123",
            }])
            result = archive.main([
                "--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run", "--retention-days", "0",
            ])
            self.assertEqual(result, 1)

    def test_main_puts_warnings_in_the_job_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample_final").mkdir()
            def warn(*a, **k):
                archive.log.warning("something to act on")
                return []
            with patch.object(archive, "discover_folders", side_effect=warn), \
                 patch.object(archive, "set_job_comment") as set_comment:
                result = archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
            self.assertEqual(result, 1)
            messages, log_path = set_comment.call_args[0]
            self.assertEqual(messages, ["WARNING something to act on"])
            self.assertEqual(log_path, Path(tmp) / ".freezer" / "archive.log")

    def test_main_leaves_the_job_comment_alone_on_a_clean_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample_final").mkdir()
            with patch.object(archive, "set_job_comment") as set_comment:
                archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
            set_comment.assert_not_called()

    def test_messages_from_an_earlier_run_do_not_fail_a_later_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample_final").mkdir()
            with patch.object(archive, "discover_folders", side_effect=RuntimeError("boom")), \
                 patch.object(archive, "set_job_comment"), self.assertLogs(archive.log, level="ERROR"):
                archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
            result = archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
            self.assertEqual(result, 0)


class SetJobCommentTests(unittest.TestCase):
    """set_job_comment() writes the failure mail's body - only under an freezer_backup scrontab job."""

    SCRON_ENV = {"SLURM_JOB_ID": "123", "SLURM_JOB_NAME": "freezer_backup-abc123"}

    def _set(self, messages, env):
        with patch.dict(os.environ, env, clear=True), \
             patch.object(archive.subprocess, "run") as run, \
             patch.object(archive.time, "sleep") as sleep:
            run.return_value.returncode = 0
            archive.set_job_comment(messages, Path("/nb/proj/.freezer/archive.log"))
        return run, sleep

    def test_sets_the_comment_then_pauses_under_a_scron_job(self):
        run, sleep = self._set(["WARNING a", "ERROR b"], self.SCRON_ENV)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["scontrol", "update", "JobId=123"])
        self.assertEqual(argv[3], "Comment=2 problem(s) archiving /nb/proj, full log: /nb/proj/.freezer/archive.log"
                                  "\nWARNING a\nERROR b")
        sleep.assert_called_once_with(archive.COMMENT_SETTLE_SECONDS)

    def test_does_nothing_outside_slurm(self):
        run, sleep = self._set(["WARNING a"], {})
        run.assert_not_called()
        sleep.assert_not_called()

    def test_does_nothing_in_some_other_job(self):
        # e.g. a hand run from an interactive session - don't overwrite that job's comment
        run, _ = self._set(["WARNING a"], {"SLURM_JOB_ID": "123", "SLURM_JOB_NAME": "interactive"})
        run.assert_not_called()

    def test_comment_is_never_over_the_scontrol_limit(self):
        # 1024 bytes: `scontrol update Comment=` rejects anything longer outright, leaving no mail body at all
        for messages in (["WARNING " + "é" * 5000],
                         [f"WARNING message {i} " + "x" * 250 for i in range(50)]):
            run, _ = self._set(messages, self.SCRON_ENV)
            comment = run.call_args[0][0][3][len("Comment="):]
            self.assertLessEqual(len(comment.encode()), archive.MAX_COMMENT_BYTES)

    def test_scontrol_failure_is_logged_without_pausing(self):
        with patch.dict(os.environ, self.SCRON_ENV, clear=True), \
             patch.object(archive.subprocess, "run") as run, \
             patch.object(archive.time, "sleep") as sleep, \
             self.assertLogs(archive.log, level="ERROR") as cm:
            run.return_value.returncode = 1
            run.return_value.stderr = "Access/permission denied"
            archive.set_job_comment(["WARNING a"], Path("/nb/proj/.freezer/archive.log"))
        self.assertIn("Access/permission denied", cm.output[0])
        sleep.assert_not_called()

    def test_main_logs_unexpected_exception_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample_final").mkdir()
            with patch.object(archive, "discover_folders", side_effect=RuntimeError("boom")):
                with self.assertLogs(archive.log, level="ERROR") as cm:
                    result = archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
            self.assertEqual(result, 1)
            self.assertTrue(any("unexpected error" in msg for msg in cm.output))

    def test_main_still_releases_lock_after_unexpected_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample_final").mkdir()
            with patch.object(archive, "discover_folders", side_effect=RuntimeError("boom")):
                with self.assertLogs(archive.log, level="ERROR"):
                    archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
            # lock released - a follow-up run can acquire it immediately
            fh = archive.acquire_lock(Path(tmp) / ".freezer" / "lock")
            fh.close()


class CommentBodyTests(unittest.TestCase):
    LOG = Path("/nb/proj/.freezer/archive.log")

    def test_one_huge_message_is_cut_short_not_dropped(self):
        body = archive.comment_body(["WARNING " + "x" * 5000], self.LOG).splitlines()
        self.assertEqual(len(body), 2)
        self.assertTrue(body[1].startswith("WARNING xxx"))
        self.assertTrue(body[1].endswith("..."))
        self.assertLessEqual(len(body[1].encode()), archive.MAX_MESSAGE_BYTES)

    def test_messages_that_do_not_fit_are_counted(self):
        messages = [f"WARNING message {i:02d} " + "x" * 200 for i in range(20)]
        body = archive.comment_body(messages, self.LOG).splitlines()
        shown = body[1:-1]
        self.assertEqual(shown, messages[:len(shown)])
        self.assertEqual(body[-1], f"... and {20 - len(shown)} more")
        self.assertLessEqual(len("\n".join(body).encode()), archive.MAX_COMMENT_BYTES)

    def test_multibyte_characters_are_not_split(self):
        body = archive.comment_body(["WARNING " + "é" * 5000], self.LOG)
        body.encode().decode()  # no half characters
        self.assertLessEqual(len(body.encode()), archive.MAX_COMMENT_BYTES)

    def test_traceback_is_left_out_of_the_message(self):
        collector = archive.MessageCollector()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            record = archive.log.makeRecord(archive.log.name, archive.logging.ERROR, __file__, 0,
                                            "unexpected error", (), sys.exc_info())
        collector.handle(record)
        self.assertEqual(collector.messages, ["ERROR unexpected error"])


class IdempotencyTests(unittest.TestCase):
    # PLAN.md Phase 6: run twice, no duplicate tars/entries. Now that
    # append_metadata() actually persists, the second run's unarchived_files()
    # should see the file as already archived and skip tarchive() entirely.
    @patch.object(archive, "tarchive")
    def test_running_twice_only_tarchives_once_now_metadata_persists(self, mock_tar):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            (folder / "a.txt").write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"

            mock_tar.return_value = [
                {
                    "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-20260101",
                    "size": 4, "mtime": 0.0, "checksum": "abc123",
                },
            ]

            archive.process_folder(folder, "test-bucket", metadata_path)
            archive.process_folder(folder, "test-bucket", metadata_path)

            self.assertEqual(mock_tar.call_count, 1)


class NoMatchesLoggingTests(unittest.TestCase):
    def test_main_logs_when_no_folders_match_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            pattern = str(Path(tmp) / "*_final")
            with self.assertLogs(archive.log, level="INFO") as cm:
                archive.main(["--pattern", pattern, "--bucket", "b", "--dry-run"])
            self.assertTrue(any("no folders matched pattern" in msg for msg in cm.output))


class RelativePatternRunsAgainstCwdTests(unittest.TestCase):
    def test_relative_pattern_discovers_folder_relative_to_cwd(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample_final").mkdir()
            (Path(tmp) / "sample_final" / "a.txt").write_text("data")
            original_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with self.assertLogs(archive.log, level="INFO") as cm:
                    archive.main(["--pattern", "*_final", "--bucket", "b", "--dry-run"])
            finally:
                os.chdir(original_cwd)
            self.assertTrue(any("would tar" in msg and "would be recorded as archived" in msg for msg in cm.output))
            # state dir lands under cwd's ./sample_final's parent (".") too
            self.assertTrue((Path(tmp) / ".freezer").is_dir())


# Resumability: a crash between writing the tar and appending its metadata
# record is a known, unresolved gap (SPEC Open Questions) - the next run
# would re-archive into a second tar rather than reconciling against
# what's already on Freezer. Add a test once that's actually built.


class DryRunTests(unittest.TestCase):
    @patch.object(archive, "tarchive")
    def test_dry_run_does_not_tar_record_or_touch(self, mock_tar):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            old_time = 1000000
            import os
            os.utime(path, (old_time, old_time))
            metadata_path = Path(tmp) / "metadata.jsonl"

            archive.process_folder(folder, "test-bucket", metadata_path, dry_run=True)

            mock_tar.assert_not_called()
            self.assertEqual(archive.load_metadata(metadata_path), {})
            self.assertEqual(path.stat().st_mtime, old_time)

    def test_dry_run_with_no_new_files_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.process_folder(folder, "test-bucket", metadata_path, dry_run=True)
            self.assertEqual(archive.load_metadata(metadata_path), {})

    def test_dry_run_reports_tar_and_archive_counts_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            (folder / "a.txt").write_text("data")
            (folder / "b.txt").write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"

            with self.assertLogs(archive.log, level="INFO") as cm:
                archive.process_folder(folder, "test-bucket", metadata_path, dry_run=True)

            self.assertTrue(any(
                "would tar 2 new file(s)" in msg and "2 would be recorded as archived" in msg
                for msg in cm.output
            ))


class DriftTests(unittest.TestCase):
    def _archived_with(self, folder_name, filename, mtime):
        return {
            (folder_name, filename): {
                "folder": folder_name, "file": filename, "tar_name": "x-1",
                "size": 4, "mtime": mtime, "checksum": "abc123",
            },
        }

    def test_unmodified_file_is_not_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            archived = self._archived_with("sample_final", "a.txt", path.stat().st_mtime)
            self.assertEqual(archive.detect_drift(folder, archived), [])

    def test_modified_file_is_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            archived = self._archived_with("sample_final", "a.txt", path.stat().st_mtime - 100)
            self.assertEqual(archive.detect_drift(folder, archived), [path])

    def test_deleted_file_is_not_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            archived = self._archived_with("sample_final", "gone.txt", 0.0)
            self.assertEqual(archive.detect_drift(folder, archived), [])

    def test_other_folders_records_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            archived = self._archived_with("other_final", "a.txt", 0.0)
            self.assertEqual(archive.detect_drift(folder, archived), [])


class ProcessFolderDriftTests(unittest.TestCase):
    def test_drifted_file_without_overwrite_is_logged_but_not_rearchived(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [{
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                "size": 4, "mtime": path.stat().st_mtime - 100, "checksum": "abc123",
            }])

            with patch.object(archive, "tarchive") as mock_tar:
                with self.assertLogs(archive.log, level="WARNING") as cm:
                    archive.process_folder(folder, "test-bucket", metadata_path)

            mock_tar.assert_not_called()
            self.assertEqual(len(cm.output), 1)  # one summary per folder, not one per file
            self.assertIn("1 file(s) in sample_final changed since they were archived (a.txt)", cm.output[0])
            self.assertIn("--overwrite", cm.output[0])

    def test_drift_warning_names_the_first_few_files_and_counts_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            (folder / "sub").mkdir(parents=True)
            names = ["a.txt", "b.txt", "c.txt", "d.txt", "sub/e.txt"]
            records = []
            for name in names:
                (folder / name).write_text("data")
                records.append({"folder": "sample_final", "file": name, "tar_name": "sample_final-old",
                                "size": 4, "mtime": (folder / name).stat().st_mtime - 100, "checksum": "x"})
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, records)

            with patch.object(archive, "tarchive"), self.assertLogs(archive.log, level="WARNING") as cm:
                archive.process_folder(folder, "test-bucket", metadata_path)

            self.assertIn("5 file(s) in sample_final changed since they were archived "
                          "(a.txt, b.txt, c.txt, +2 more)", cm.output[0])

    def _completed(self, stdout=""):
        return type("CompletedProcess", (), {"stdout": stdout, "returncode": 0})()

    def test_drifted_file_with_overwrite_is_rearchived(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [{
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                "size": 4, "mtime": path.stat().st_mtime - 100, "checksum": "abc123",
            }])
            new_record = {
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-new",
                "size": 4, "mtime": path.stat().st_mtime, "checksum": "def456",
            }
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                return self._completed()

            with patch.object(archive, "tarchive", return_value=[new_record]) as mock_tar:
                with patch.object(archive.subprocess, "run", side_effect=fake_run):
                    with self.assertLogs(archive.log, level="INFO") as cm:
                        archive.process_folder(folder, "test-bucket", metadata_path, overwrite=True)

            mock_tar.assert_called_once()
            # INFO, not WARNING - --overwrite is a standing instruction, so
            # this shouldn't count as something needing an alert.
            self.assertTrue(all(r.levelname != "WARNING" for r in cm.records))
            new_files_arg = mock_tar.call_args[0][1]
            self.assertIn(path, new_files_arg)

            # the old tar had no other occupants, so it's deleted outright
            # rather than left for retention to age out.
            self.assertIn(["s3cmd", "del", "s3://test-bucket/sample_final-old"], calls)
            self.assertEqual(archive.load_deleted_tars(metadata_path), {"sample_final-old"})

    def test_overwrite_carries_siblings_forward_and_deletes_old_tar(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            a_path = folder / "a.txt"
            a_path.write_text("data")
            b_path = folder / "b.txt"
            b_path.write_text("other data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                 "size": 4, "mtime": a_path.stat().st_mtime - 100, "checksum": "abc123"},
                # b.txt is untouched since it was archived - not drifted, but it
                # shares a_path's old tar and must be carried along.
                {"folder": "sample_final", "file": "b.txt", "tar_name": "sample_final-old",
                 "size": 10, "mtime": b_path.stat().st_mtime, "checksum": "xyz789"},
            ])
            new_records = [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-new",
                 "size": 4, "mtime": a_path.stat().st_mtime, "checksum": "def456"},
                {"folder": "sample_final", "file": "b.txt", "tar_name": "sample_final-new",
                 "size": 10, "mtime": b_path.stat().st_mtime, "checksum": "xyz789"},
            ]
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                return self._completed()

            with patch.object(archive, "tarchive", return_value=new_records) as mock_tar:
                with patch.object(archive.subprocess, "run", side_effect=fake_run):
                    archive.process_folder(folder, "test-bucket", metadata_path, overwrite=True)

            new_files_arg = mock_tar.call_args[0][1]
            self.assertIn(a_path, new_files_arg)
            self.assertIn(b_path, new_files_arg)

            self.assertIn(["s3cmd", "del", "s3://test-bucket/sample_final-old"], calls)
            archived = archive.load_metadata(metadata_path)
            self.assertEqual(archived[("sample_final", "a.txt")]["tar_name"], "sample_final-new")
            self.assertEqual(archived[("sample_final", "b.txt")]["tar_name"], "sample_final-new")

    def test_overwrite_leaves_old_tar_when_sibling_missing_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            a_path = folder / "a.txt"
            a_path.write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                 "size": 4, "mtime": a_path.stat().st_mtime - 100, "checksum": "abc123"},
                # b.txt was archived before but has since been deleted from disk.
                {"folder": "sample_final", "file": "b.txt", "tar_name": "sample_final-old",
                 "size": 10, "mtime": 0.0, "checksum": "xyz789"},
            ])
            new_record = [{
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-new",
                "size": 4, "mtime": a_path.stat().st_mtime, "checksum": "def456",
            }]

            with patch.object(archive, "tarchive", return_value=new_record):
                with patch.object(archive.subprocess, "run") as run:
                    with self.assertLogs(archive.log, level="WARNING") as cm:
                        archive.process_folder(folder, "test-bucket", metadata_path, overwrite=True)

            run.assert_not_called()
            self.assertTrue(any("no longer on disk" in msg for msg in cm.output))
            self.assertEqual(
                archive.load_all_tar_names(metadata_path), {"sample_final-old", "sample_final-new"}
            )
            self.assertNotIn("sample_final-old", archive.load_deleted_tars(metadata_path))

    def test_overwrite_unconfigured_s3cmd_on_del_does_not_record_a_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [{
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                "size": 4, "mtime": path.stat().st_mtime - 100, "checksum": "abc123",
            }])
            new_record = {
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-new",
                "size": 4, "mtime": path.stat().st_mtime, "checksum": "def456",
            }
            error = archive.subprocess.CalledProcessError(78, ["s3cmd"])

            with patch.object(archive, "tarchive", return_value=[new_record]):
                with patch.object(archive.subprocess, "run", side_effect=error):
                    with self.assertLogs(archive.log, level="ERROR"):
                        archive.process_folder(folder, "test-bucket", metadata_path, overwrite=True)

            self.assertEqual(archive.load_deleted_tars(metadata_path), set())
            self.assertIn("sample_final-old", archive.load_all_tar_names(metadata_path))

    def test_overwrite_dry_run_logs_would_delete_and_makes_no_s3_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [{
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                "size": 4, "mtime": path.stat().st_mtime - 100, "checksum": "abc123",
            }])

            with patch.object(archive.subprocess, "run") as run:
                with self.assertLogs(archive.log, level="INFO") as cm:
                    archive.process_folder(
                        folder, "test-bucket", metadata_path, overwrite=True, dry_run=True,
                    )

            run.assert_not_called()
            self.assertTrue(
                any("would delete superseded archive sample_final-old" in msg for msg in cm.output)
            )

    def test_no_drift_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [{
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                "size": 4, "mtime": path.stat().st_mtime, "checksum": "abc123",
            }])

            with self.assertLogs(archive.log, level="INFO") as cm:
                archive.process_folder(folder, "test-bucket", metadata_path)

            self.assertFalse(any("changed since it was archived" in msg for msg in cm.output))


class RetentionDeletionTests(unittest.TestCase):
    def _completed(self, stdout=""):
        return type("CompletedProcess", (), {"stdout": stdout, "returncode": 0})()

    def _write_archived_file_record(self, metadata_path, tar_name):
        archive.append_metadata(metadata_path, [
            {
                "folder": "sample_final", "file": "a.txt", "tar_name": tar_name,
                "size": 4, "mtime": 0.0, "checksum": "abc",
            },
        ])

    def test_list_bucket_objects_parses_dates(self):
        stdout = (
            "2026-01-01 12:00  1234  s3://bucket/a-20260101.tar\n"
            "2026-02-02 12:00  5678  s3://bucket/b-20260202.tar.gz\n"
        )
        with patch.object(archive.subprocess, "run", return_value=self._completed(stdout)):
            objects = archive.list_bucket_objects("bucket")
        self.assertEqual(
            objects,
            {"a-20260101.tar": date(2026, 1, 1), "b-20260202.tar.gz": date(2026, 2, 2)},
        )

    def test_list_bucket_objects_returns_none_when_unconfigured(self):
        error = archive.subprocess.CalledProcessError(78, ["s3cmd"])
        with patch.object(archive.subprocess, "run", side_effect=error):
            with self.assertLogs(archive.log, level="WARNING"):
                self.assertIsNone(archive.list_bucket_objects("bucket"))

    def test_load_deleted_tars_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            self.assertEqual(archive.load_deleted_tars(path), set())

    def test_load_deleted_tars_collects_deleted_events_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(path, [
                {"folder": "x_final", "file": "a.txt", "tar_name": "x-1"},
                {"event": "deleted", "tar_name": "x-1", "date": "2026-01-01"},
            ])
            self.assertEqual(archive.load_deleted_tars(path), {"x-1"})

    def test_load_all_tar_names_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            self.assertEqual(archive.load_all_tar_names(path), set())

    def test_load_all_tar_names_includes_a_superseded_tar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(path, [
                {"folder": "x_final", "file": "a.txt", "tar_name": "x-old"},
                {"folder": "x_final", "file": "a.txt", "tar_name": "x-new"},  # same file, re-archived
            ])
            self.assertEqual(archive.load_all_tar_names(path), {"x-old", "x-new"})

    def test_load_all_tar_names_excludes_deleted_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(path, [
                {"event": "deleted", "tar_name": "x-1", "date": "2026-01-01"},
            ])
            self.assertEqual(archive.load_all_tar_names(path), set())

    def test_retention_days_zero_disables_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            self._write_archived_file_record(metadata_path, "sample_final-old.tar")
            with patch.object(archive.subprocess, "run") as run:
                archive.delete_expired_archives("bucket", metadata_path, retention_days=0)
            run.assert_not_called()

    def test_no_archived_tars_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            with patch.object(archive.subprocess, "run") as run:
                archive.delete_expired_archives("bucket", metadata_path)
            run.assert_not_called()

    def test_expired_tar_without_delete_expired_warns_and_does_not_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)
            archive_date = date.today() - timedelta(days=800)

            def fake_run(args, **kwargs):
                self.assertEqual(args[1], "ls")  # a "del" call here means the default isn't warn-only
                return self._completed(f"{archive_date.isoformat()} 12:00  1234  s3://bucket/{tar_name}\n")

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                with self.assertLogs(archive.log, level="WARNING") as cm:
                    archive.delete_expired_archives("bucket", metadata_path, retention_days=730)

            self.assertTrue(any(tar_name in msg and "--delete-expired" in msg for msg in cm.output))
            self.assertEqual(archive.load_deleted_tars(metadata_path), set())

    def test_several_expired_tars_log_one_summary_warning(self):
        # An expired tar stays expired every run - one WARNING per tar per
        # run would be noise, so they're summarised into one line.
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [
                {"folder": "sample_final", "file": f"{n}.txt", "tar_name": f"sample_final-{n}.tar",
                 "size": 4, "mtime": 0.0, "checksum": "abc"}
                for n in (1, 2, 3)
            ])
            listing = "".join(
                f"{(date.today() - timedelta(days=days)).isoformat()} 12:00  1234  s3://bucket/sample_final-{n}.tar\n"
                for n, days in ((1, 800), (2, 900), (3, 740))
            )

            with patch.object(archive.subprocess, "run", return_value=self._completed(listing)):
                with self.assertLogs(archive.log, level="WARNING") as cm:
                    archive.delete_expired_archives("bucket", metadata_path, retention_days=730)

            self.assertEqual(len(cm.output), 1)
            self.assertIn("3 archive(s)", cm.output[0])
            self.assertIn("sample_final-2.tar", cm.output[0])  # the oldest
            self.assertEqual(archive.load_deleted_tars(metadata_path), set())

    def test_tar_missing_from_bucket_is_recorded_even_without_delete_expired(self):
        # Recording a hand-deletion is bookkeeping, not a deletion - it
        # doesn't need the flag.
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)

            with patch.object(archive.subprocess, "run", return_value=self._completed("")):
                archive.delete_expired_archives("bucket", metadata_path, retention_days=730)

            self.assertEqual(archive.load_deleted_tars(metadata_path), {tar_name})

    def test_unconfigured_s3cmd_on_del_does_not_record_a_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)
            archive_date = date.today() - timedelta(days=800)

            def fake_run(args, **kwargs):
                if args[1] == "ls":
                    return self._completed(f"{archive_date.isoformat()} 12:00  1234  s3://bucket/{tar_name}\n")
                raise archive.subprocess.CalledProcessError(78, args)

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                with self.assertLogs(archive.log, level="ERROR"):
                    archive.delete_expired_archives(
                        "bucket", metadata_path, retention_days=730, delete_expired=True
                    )

            self.assertEqual(archive.load_deleted_tars(metadata_path), set())

    def test_tar_exactly_at_retention_boundary_is_deleted(self):
        # age_days == retention_days exactly (730): the boundary must count
        # as expired, not "one day short".
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)
            archive_date = date.today() - timedelta(days=730)
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if args[1] == "ls":
                    return self._completed(f"{archive_date.isoformat()} 12:00  1234  s3://bucket/{tar_name}\n")
                return self._completed()

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                archive.delete_expired_archives(
                    "bucket", metadata_path, retention_days=730, delete_expired=True
                )

            self.assertTrue(any(c[1] == "del" for c in calls))
            self.assertEqual(archive.load_deleted_tars(metadata_path), {tar_name})

    def test_tar_one_day_short_of_retention_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)
            archive_date = date.today() - timedelta(days=729)
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                return self._completed(f"{archive_date.isoformat()} 12:00  1234  s3://bucket/{tar_name}\n")

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                with patch.object(archive.log, "warning") as warning:  # assertNoLogs is 3.10+
                    archive.delete_expired_archives(
                        "bucket", metadata_path, retention_days=730, delete_expired=True
                    )

            warning.assert_not_called()
            self.assertFalse(any(c[1] == "del" for c in calls))
            self.assertEqual(archive.load_deleted_tars(metadata_path), set())

    def test_tar_past_retention_is_deleted_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)
            archive_date = date.today() - timedelta(days=800)
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if args[1] == "ls":
                    return self._completed(f"{archive_date.isoformat()} 12:00  1234  s3://bucket/{tar_name}\n")
                if args[1] == "del":
                    return self._completed()
                raise AssertionError(f"unexpected call: {args}")

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                archive.delete_expired_archives(
                    "bucket", metadata_path, retention_days=730, delete_expired=True
                )

            self.assertTrue(any(c[1] == "del" and c[-1] == f"s3://bucket/{tar_name}" for c in calls))
            self.assertEqual(archive.load_deleted_tars(metadata_path), {tar_name})

    def test_tar_superseded_by_overwrite_is_still_a_deletion_candidate(self):
        # Regression test: load_metadata() dedupes to one record per
        # (folder, file), so a tar superseded by a later --overwrite
        # re-archive of the same file used to vanish from
        # delete_expired_archives()'s candidate set entirely and could
        # never be retention-deleted, even though it still exists in
        # Freezer. load_all_tar_names() must see both tar names.
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            old_tar, new_tar = "sample_final-old.tar", "sample_final-new.tar"
            archive.append_metadata(metadata_path, [
                {"folder": "sample_final", "file": "a.txt", "tar_name": old_tar,
                 "size": 4, "mtime": 0.0, "checksum": "abc"},
                {"folder": "sample_final", "file": "a.txt", "tar_name": new_tar,  # supersedes old_tar
                 "size": 5, "mtime": 100.0, "checksum": "def"},
            ])
            # load_metadata()'s deduped view only knows about new_tar
            self.assertEqual(
                {r["tar_name"] for r in archive.load_metadata(metadata_path).values()}, {new_tar},
            )
            # ...but load_all_tar_names() must still see the superseded one
            self.assertEqual(archive.load_all_tar_names(metadata_path), {old_tar, new_tar})

            archive_date = date.today() - timedelta(days=800)
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if args[1] == "ls":
                    return self._completed(
                        f"{archive_date.isoformat()} 12:00  1234  s3://bucket/{old_tar}\n"
                        f"{archive_date.isoformat()} 12:00  1234  s3://bucket/{new_tar}\n"
                    )
                if args[1] == "del":
                    return self._completed()
                raise AssertionError(f"unexpected call: {args}")

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                archive.delete_expired_archives(
                    "bucket", metadata_path, retention_days=730, delete_expired=True
                )

            deleted_names = {c[-1].rsplit("/", 1)[-1] for c in calls if c[1] == "del"}
            self.assertEqual(deleted_names, {old_tar, new_tar})
            self.assertEqual(archive.load_deleted_tars(metadata_path), {old_tar, new_tar})

    def test_already_deleted_tar_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)
            archive.append_metadata(metadata_path, [
                {"event": "deleted", "tar_name": tar_name, "date": "2020-01-01"},
            ])

            with patch.object(archive.subprocess, "run") as run:
                archive.delete_expired_archives(
                    "bucket", metadata_path, retention_days=730, delete_expired=True
                )
            run.assert_not_called()

    def test_tar_missing_from_bucket_is_recorded_without_s3cmd_del(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)

            def fake_run(args, **kwargs):
                self.assertEqual(args[1], "ls")
                return self._completed("")  # bucket listing succeeded but tar_name isn't in it

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                archive.delete_expired_archives(
                    "bucket", metadata_path, retention_days=730, delete_expired=True
                )

            self.assertEqual(archive.load_deleted_tars(metadata_path), {tar_name})

    def test_dry_run_does_not_delete_or_write_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)
            archive_date = date.today() - timedelta(days=800)

            def fake_run(args, **kwargs):
                self.assertEqual(args[1], "ls")  # a "del" call here would be a --dry-run bug
                return self._completed(f"{archive_date.isoformat()} 12:00  1234  s3://bucket/{tar_name}\n")

            with patch.object(archive.subprocess, "run", side_effect=fake_run):
                archive.delete_expired_archives(
                    "bucket", metadata_path, retention_days=730, delete_expired=True, dry_run=True
                )

            self.assertEqual(archive.load_deleted_tars(metadata_path), set())

    def test_listing_failure_does_not_mark_everything_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            tar_name = "sample_final-old.tar"
            self._write_archived_file_record(metadata_path, tar_name)
            error = archive.subprocess.CalledProcessError(78, ["s3cmd"])

            with patch.object(archive.subprocess, "run", side_effect=error):
                with self.assertLogs(archive.log, level="WARNING"):
                    archive.delete_expired_archives(
                        "bucket", metadata_path, retention_days=730, delete_expired=True
                    )

            self.assertEqual(archive.load_deleted_tars(metadata_path), set())


class ParseArgsTests(unittest.TestCase):
    def test_dry_run_flag_defaults_false(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertFalse(args.dry_run)

    def test_dry_run_long_flag_is_parsed(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_dry_run_short_flag_is_parsed(self):
        args = archive.parse_args(["-p", "/nb/*_final", "-b", "b", "-n"])
        self.assertTrue(args.dry_run)

    def test_overwrite_flag_defaults_false(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertFalse(args.overwrite)

    def test_overwrite_long_flag_is_parsed(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b", "--overwrite"])
        self.assertTrue(args.overwrite)

    def test_overwrite_short_flag_is_parsed(self):
        args = archive.parse_args(["-p", "/nb/*_final", "-b", "b", "-o"])
        self.assertTrue(args.overwrite)

    def test_missing_pattern_exits(self):
        with self.assertRaises(SystemExit):
            archive.parse_args(["--bucket", "b"])

    def test_relative_pattern_is_accepted_unchanged(self):
        args = archive.parse_args(["--pattern", "*_final", "--bucket", "b"])
        self.assertEqual(args.pattern, "*_final")

    def test_stray_positional_between_flags_exits_instead_of_swallowing_rest(self):
        # getopt stops parsing options at the first non-option argument, so
        # a stray word here would otherwise silently drop --bucket entirely.
        with self.assertRaises(SystemExit):
            archive.parse_args(["--pattern", "/nb/*_final", "stray", "--bucket", "b"])

    def test_retention_days_defaults(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertEqual(args.retention_days, archive.DEFAULT_RETENTION_DAYS)

    def test_retention_days_short_flag_is_parsed(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b", "-r", "365"])
        self.assertEqual(args.retention_days, 365)

    def test_retention_days_long_flag_is_parsed(self):
        args = archive.parse_args(
            ["--pattern", "/nb/*_final", "--bucket", "b", "--retention-days", "0"]
        )
        self.assertEqual(args.retention_days, 0)

    def test_retention_days_non_integer_exits(self):
        with self.assertRaises(SystemExit):
            archive.parse_args(
                ["--pattern", "/nb/*_final", "--bucket", "b", "--retention-days", "forever"]
            )

    def test_retention_days_negative_exits(self):
        with self.assertRaises(SystemExit):
            archive.parse_args(
                ["--pattern", "/nb/*_final", "--bucket", "b", "--retention-days", "-1"]
            )

    def test_delete_expired_defaults_false(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertFalse(args.delete_expired)

    def test_delete_expired_long_flag_is_parsed(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b", "--delete-expired"])
        self.assertTrue(args.delete_expired)

    def test_delete_expired_short_flag_is_parsed(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b", "-d"])
        self.assertTrue(args.delete_expired)

    def test_log_level_defaults_to_info(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b"])
        self.assertEqual(args.log_level, "INFO")

    def test_log_level_long_flag_is_parsed(self):
        args = archive.parse_args(
            ["--pattern", "/nb/*_final", "--bucket", "b", "--log-level", "DEBUG"]
        )
        self.assertEqual(args.log_level, "DEBUG")

    def test_log_level_short_flag_is_parsed(self):
        args = archive.parse_args(["-p", "/nb/*_final", "-b", "b", "-l", "ERROR"])
        self.assertEqual(args.log_level, "ERROR")

    def test_log_level_is_case_insensitive(self):
        args = archive.parse_args(
            ["--pattern", "/nb/*_final", "--bucket", "b", "--log-level", "warning"]
        )
        self.assertEqual(args.log_level, "WARNING")

    def test_log_level_invalid_value_exits(self):
        with self.assertRaises(SystemExit):
            archive.parse_args(
                ["--pattern", "/nb/*_final", "--bucket", "b", "--log-level", "VERBOSE"]
            )

    def test_mail_user_is_not_a_runner_flag(self):
        # mail is set on the scrontab entry's #SCRON directive, not passed to the runner
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b", "--mail-user", "a@example.com"])


DAY = 86400


class TouchTests(unittest.TestCase):
    """
    The cleaner goes by atime and ctime, and ctime can't be backdated (every
    utime() call resets it), so these move `now` forward instead of
    backdating files.
    """

    def _folder(self, tmp, *names):
        folder = Path(tmp) / "sample_final"
        folder.mkdir()
        for name in names:
            (folder / name).write_text("x")
        return folder

    def _last_activity(self, path):
        st = path.stat()
        return max(st.st_atime, st.st_ctime)

    def test_touches_unarchived_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "pending.txt", "done.txt")
            pending, done = folder / "pending.txt", folder / "done.txt"
            archived = {("sample_final", "done.txt"): {"folder": "sample_final", "file": "done.txt"}}
            with patch.object(archive.subprocess, "run", return_value=self._completed()) as run:
                archive.touch_pending(folder, archived, now=time.time() + 100 * DAY)
            touched = run.call_args[0][0]
            self.assertEqual(touched[:4], ["touch", "-a", "-c", "--"])
            self.assertIn(pending, touched)
            self.assertNotIn(done, touched)

    def test_default_threshold_is_60_days_of_atime_and_ctime(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "a.txt")
            last = self._last_activity(folder / "a.txt")
            with patch.object(archive.subprocess, "run", return_value=self._completed()) as run:
                archive.touch_pending(folder, {}, now=last + 59.9 * DAY)
            run.assert_not_called()
            with patch.object(archive.subprocess, "run", return_value=self._completed()) as run:
                archive.touch_pending(folder, {}, now=last + 60 * DAY)
            run.assert_called_once()

    def test_old_mtime_alone_does_not_trigger_a_touch(self):
        # mtime isn't one of the cleaner's clocks.
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "a.txt")
            path = folder / "a.txt"
            os.utime(path, (time.time(), time.time() - 1000 * DAY))
            with patch.object(archive.subprocess, "run") as run:
                archive.touch_pending(folder, {})
            run.assert_not_called()

    def test_recent_atime_counts_even_with_old_ctime(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "a.txt")
            path = folder / "a.txt"
            now = time.time() + 100 * DAY  # ctime (~real now) is 100 days old by then...
            os.utime(path, (now - 10 * DAY, path.stat().st_mtime))  # ...but atime only 10
            with patch.object(archive.subprocess, "run") as run:
                archive.touch_pending(folder, {}, now=now)
            run.assert_not_called()

    def test_real_touch_refreshes_atime_and_ctime_but_keeps_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "a.txt")
            path = folder / "a.txt"
            old = time.time() - 100 * DAY
            os.utime(path, (old, old))
            before = path.stat()
            time.sleep(0.05)
            archive.touch_pending(folder, {}, now=time.time() + 100 * DAY)
            after = path.stat()
            self.assertEqual(after.st_mtime, before.st_mtime)
            self.assertGreater(after.st_atime, old)
            self.assertGreater(after.st_ctime, before.st_ctime)

    def test_dry_run_does_not_touch_and_logs_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "a.txt")
            with patch.object(archive.subprocess, "run") as run:
                with self.assertLogs(archive.log, level="INFO") as cm:
                    archive.touch_pending(folder, {}, dry_run=True, now=time.time() + 100 * DAY)
            run.assert_not_called()
            self.assertTrue(any("would touch 1 pending file" in msg for msg in cm.output))

    def test_touches_a_stale_extra_path_too(self):
        # extra_paths covers a drifted-but-left-as-is file (still present
        # in `archived`, so unarchived_files() alone would never see it -
        # see process_folder()).
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "drifted.txt")
            drifted = folder / "drifted.txt"
            archived = {("sample_final", "drifted.txt"): {"folder": "sample_final", "file": "drifted.txt"}}
            with patch.object(archive.subprocess, "run", return_value=self._completed()) as run:
                archive.touch_pending(folder, archived, extra_paths=[drifted], now=time.time() + 100 * DAY)
            self.assertIn(drifted, run.call_args[0][0])

    def test_extra_path_still_respects_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "drifted.txt")
            drifted = folder / "drifted.txt"
            archived = {("sample_final", "drifted.txt"): {"folder": "sample_final", "file": "drifted.txt"}}
            with patch.object(archive.subprocess, "run") as run:
                archive.touch_pending(folder, archived, extra_paths=[drifted])
            run.assert_not_called()

    def test_vanished_file_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "a.txt")
            gone = folder / "gone.txt"  # e.g. a drifted file deleted since detect_drift()
            with patch.object(archive.subprocess, "run", return_value=self._completed()) as run:
                with self.assertLogs(archive.log, level="INFO") as cm:
                    archive.touch_pending(folder, {}, extra_paths=[gone], now=time.time() + 100 * DAY)
            self.assertTrue(any("vanished before it could be touched" in msg for msg in cm.output))
            self.assertNotIn(gone, run.call_args[0][0])

    def test_permission_failure_warns_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, "a.txt", "b.txt")
            failed = self._completed(
                returncode=1, stderr=f"touch: setting times of '{folder}/b.txt': Permission denied\n",
            )
            with patch.object(archive.subprocess, "run", return_value=failed):
                with self.assertLogs(archive.log, level="INFO") as cm:
                    archive.touch_pending(folder, {}, now=time.time() + 100 * DAY)
            warnings = [r.getMessage() for r in cm.records if r.levelname == "WARNING"]
            self.assertEqual(len(warnings), 1)
            self.assertIn("could not touch 1 pending file(s)", warnings[0])
            self.assertIn("b.txt", warnings[0])
            self.assertTrue(any("touched 1 pending file(s)" in msg for msg in cm.output))

    def test_touches_in_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp, *(f"{n}.txt" for n in range(5)))
            with patch.object(archive, "TOUCH_BATCH_SIZE", 2), \
                 patch.object(archive.subprocess, "run", return_value=self._completed()) as run:
                archive.touch_pending(folder, {}, now=time.time() + 100 * DAY)
            self.assertEqual(run.call_count, 3)  # 2 + 2 + 1
            self.assertEqual(sum(len(c[0][0]) - 4 for c in run.call_args_list), 5)

    def _completed(self, returncode=0, stderr=""):
        return type("CompletedProcess", (), {"stdout": "", "stderr": stderr, "returncode": returncode})()


class ProcessFolderTouchesStaleDriftTests(unittest.TestCase):
    def test_stale_drifted_file_left_as_is_still_gets_touched(self):
        # Regression test for the bug where a drifted file with no
        # --overwrite was permanently exempt from touch_pending's
        # staleness protection, since it's still a key in `archived`.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [{
                "folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-old",
                "size": 4, "mtime": path.stat().st_mtime - 100, "checksum": "abc123",
            }])
            future = time.time() + 100 * DAY
            completed = type("CompletedProcess", (), {"stdout": "", "stderr": "", "returncode": 0})()

            with patch.object(archive, "tarchive") as mock_tar, \
                 patch.object(archive.time, "time", return_value=future), \
                 patch.object(archive.subprocess, "run", return_value=completed) as run:
                archive.process_folder(folder, "test-bucket", metadata_path)

            mock_tar.assert_not_called()  # drift without --overwrite: left as-is, not re-archived
            self.assertIn(path, run.call_args[0][0])  # but still touched


class ManualCommandTests(unittest.TestCase):
    """Warnings quote the exact command to run by hand for the destructive step."""

    def test_default_run_gives_a_minimal_command(self):
        args = archive.parse_args(["--pattern", "/nb/*_final", "--bucket", "b", "--log-level", "DEBUG"])
        self.assertEqual(
            archive.manual_command(args, "--delete-expired"),
            "freezer_backup_runner --pattern '/nb/*_final' --bucket b --delete-expired",
        )

    def test_keeps_non_default_retention_and_compress(self):
        args = archive.parse_args(
            ["--pattern", "/nb/my data/*_final", "--bucket", "b", "-c", "never", "-r", "365"]
        )
        self.assertEqual(
            archive.manual_command(args, "--overwrite"),
            "freezer_backup_runner --pattern '/nb/my data/*_final' --bucket b --compress never "
            "--retention-days 365 --overwrite",
        )

    def test_retention_warning_quotes_the_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [
                {"folder": "x_final", "file": "a", "tar_name": "x.tar", "size": 1, "mtime": 0.0, "checksum": "c"},
            ])
            listing = f"{(date.today() - timedelta(days=800)).isoformat()} 12:00  1  s3://b/x.tar\n"
            completed = type("CompletedProcess", (), {"stdout": listing, "returncode": 0})()
            with patch.object(archive.subprocess, "run", return_value=completed):
                with self.assertLogs(archive.log, level="WARNING") as cm:
                    archive.delete_expired_archives("b", metadata_path, delete_command="CMD --delete-expired")
        self.assertIn("run: CMD --delete-expired", cm.output[0])

    def test_drift_warning_quotes_the_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "x_final"
            folder.mkdir()
            path = folder / "a.txt"
            path.write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"
            archive.append_metadata(metadata_path, [{
                "folder": "x_final", "file": "a.txt", "tar_name": "x.tar",
                "size": 4, "mtime": path.stat().st_mtime - 100, "checksum": "c",
            }])
            with self.assertLogs(archive.log, level="WARNING") as cm:
                archive.process_folder(folder, "b", metadata_path, overwrite_command="CMD --overwrite")
        self.assertIn("run: CMD --overwrite", cm.output[0])


class NeedsTouchTests(unittest.TestCase):
    """Only nobackup has an auto-cleaner - touching anywhere else would just rewrite mtimes."""

    def test_dir_under_nobackup_root_needs_touch(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "proj").mkdir()
            with patch.object(archive, "NOBACKUP_ROOT", tmp):
                self.assertTrue(archive.needs_touch(Path(tmp) / "proj"))

    def test_dir_outside_nobackup_root_does_not(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            with patch.object(archive, "NOBACKUP_ROOT", root):
                self.assertFalse(archive.needs_touch(Path(other)))

    def test_sibling_with_shared_name_prefix_does_not(self):
        # /nesi/nobackup-old must not count as under /nesi/nobackup
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "nobackup").mkdir()
            (Path(tmp) / "nobackup-old").mkdir()
            with patch.object(archive, "NOBACKUP_ROOT", str(Path(tmp) / "nobackup")):
                self.assertFalse(archive.needs_touch(Path(tmp) / "nobackup-old"))

    def test_symlink_into_nobackup_needs_touch(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            (Path(root) / "proj").mkdir()
            link = Path(other) / "link"
            link.symlink_to(Path(root) / "proj")
            with patch.object(archive, "NOBACKUP_ROOT", root):
                self.assertTrue(archive.needs_touch(link))

    def _main_touch_logs(self, nobackup_root_for):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            old = folder / "a.txt"
            old.write_text("data")
            future = time.time() + 100 * DAY  # ctime can't be backdated - move the clock instead
            with patch.object(archive, "NOBACKUP_ROOT", nobackup_root_for(tmp)), \
                 patch.object(archive.time, "time", return_value=future):
                with self.assertLogs(archive.log, level="INFO") as cm:
                    archive.main(["--pattern", f"{tmp}/*_final", "--bucket", "b", "--dry-run"])
            return [msg for msg in cm.output if "would touch" in msg]

    def test_main_touches_under_nobackup(self):
        logs = self._main_touch_logs(lambda tmp: tmp)
        self.assertTrue(any("would touch 1 pending file" in msg for msg in logs))

    def test_main_skips_touching_outside_nobackup(self):
        self.assertEqual(self._main_touch_logs(lambda tmp: "/nonexistent-nobackup-root"), [])


if __name__ == "__main__":
    unittest.main()
