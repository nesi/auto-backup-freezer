"""
Tests for runner_archive. Skeleton only - see PLAN.md Phase 6.

Uses tempfile for a fake nobackup and unittest.mock to stub out
s3cmd/tar, so these run without real Freezer access.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import runner_archive as archive  # noqa: E402


class MetadataTests(unittest.TestCase):
    def test_load_metadata_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            self.assertEqual(archive.load_metadata(path), {})

    def test_append_then_load_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.jsonl"
            record = {"folder": "x_final", "file": "a.txt", "tar_name": "x-1"}
            archive.append_metadata(path, [record])
            self.assertIn(("x_final", "a.txt"), archive.load_metadata(path))

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
            new_files = archive.diff_new_files(folder, archived={})
            self.assertEqual([p.name for p in new_files], ["a.txt"])

    def test_already_archived_file_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            (folder / "a.txt").write_text("data")
            archived = {("sample_final", "a.txt"): {"folder": "sample_final", "file": "a.txt"}}
            self.assertEqual(archive.diff_new_files(folder, archived), [])


class DiscoverFoldersTests(unittest.TestCase):
    def test_default_pattern_matches_final_suffix_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample_final").mkdir()
            (root / "sample_wip").mkdir()
            found = archive.discover_folders(root)
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
            found = archive.discover_folders(root)
            self.assertEqual([p.name for p in found], ["sample_final"])


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


class ConcurrencyGuardTests(unittest.TestCase):
    def test_second_lock_acquisition_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            fh1 = archive.acquire_lock(lock_path)
            with self.assertRaises(archive.LockHeld):
                archive.acquire_lock(lock_path)
            fh1.close()

    def test_lock_released_after_close_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "lock"
            fh1 = archive.acquire_lock(lock_path)
            fh1.close()
            fh2 = archive.acquire_lock(lock_path)
            fh2.close()


class IdempotencyTests(unittest.TestCase):
    @patch.object(archive, "tarchive")
    def test_running_twice_does_not_duplicate_metadata_entries(self, mock_tar):
        # PLAN.md Phase 6. tarchive is mocked - no real Freezer write.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            (folder / "a.txt").write_text("data")
            metadata_path = Path(tmp) / "metadata.jsonl"

            mock_tar.return_value = [
                {"folder": "sample_final", "file": "a.txt", "tar_name": "sample_final-20260101"},
            ]

            archive.process_folder(folder, "test-bucket", metadata_path)
            archive.process_folder(folder, "test-bucket", metadata_path)

            self.assertEqual(mock_tar.call_count, 1, "second run should have seen the file as already archived")


# Resumability: a crash between writing the tar and appending its metadata
# record is a known, currently-unresolved gap (SPEC Open Questions) - the
# next run would re-archive the file into a second tar rather than
# reconciling against what's already on Freezer. No test here yet because
# there's nothing implemented to test: tarchive() doesn't reconcile against
# list_freezer_contents(), and list_freezer_contents() itself isn't built.
# Add test_interrupted_run_does_not_produce_duplicate_tar once that exists.


class TouchTests(unittest.TestCase):
    def test_touch_pending_updates_mtime_of_unarchived_files_only(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "sample_final"
            folder.mkdir()
            pending = folder / "pending.txt"
            done = folder / "done.txt"
            pending.write_text("x")
            done.write_text("x")
            old_time = 1000000
            os.utime(pending, (old_time, old_time))
            os.utime(done, (old_time, old_time))

            archived = {("sample_final", "done.txt"): {"folder": "sample_final", "file": "done.txt"}}
            archive.touch_pending(folder, archived)

            self.assertNotEqual(pending.stat().st_mtime, old_time)
            self.assertEqual(done.stat().st_mtime, old_time)


if __name__ == "__main__":
    unittest.main()
