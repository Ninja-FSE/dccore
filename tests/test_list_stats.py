"""First tests for update_list.py's published statistics, and list.py's reading of them.

update_list.py was the last substantial module with no test of its own. The
master list it builds is the file every user downloads, and the two side files it
publishes alongside are what the channel advert and @<nick>-que quote.

The temp-and-swap rewrite made the LISTS survive a failed scan: they are written
to ".new" names and swapped in only once generation has succeeded, so an NFS
mount that goes away leaves the previous index exactly as it was. The two side
files were left behind by that rewrite. They were written straight to their final
names, before the guards ran, so a scan that found nothing kept the previous list
and then overwrote its published size with 0B and its byte count with 0 anyway.

The advert then announced a real file count next to 0B, or - if the write was
interrupted rather than merely wrong - nothing at all, because int("") on a
truncated rawbytes file raised and cost the caller the file count and the list
date too.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class SideFilePaths(DCCoreTestCase):
    """One definition, resolved when it is used."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def test_reader_and_writer_agree_on_the_names(self):
        """The split literal in two modules is how issue #34 happened."""
        for module in (list_mod, update_list):
            source = open(os.path.join(REPO_ROOT, f"{module.__name__}.py"),
                          encoding="utf-8").read()
            with self.subTest(module=module.__name__):
                self.assertFalse('"dccore.size.txt"' in source,
                                 "name the side files once, in config")
                self.assertFalse('"dccore.rawbytes.txt"' in source,
                                 "name the side files once, in config")

    def test_the_paths_follow_a_changed_list_directory(self):
        """!rehash reloads config; a path baked in at import would not follow."""
        config.LOCAL_LIST_DIR = os.path.join(self.tree.root, "moved")
        self.assertTrue(list_mod.size_file_path().startswith(config.LOCAL_LIST_DIR))
        self.assertTrue(list_mod.rawbytes_file_path().startswith(config.LOCAL_LIST_DIR))

    def test_the_historical_filenames_are_unchanged(self):
        """Renaming would orphan the stats on every live deployment until !update."""
        # Renamed off one operator's server name. The DOT separator is load
        # bearing: find_latest_list() globs LIST_BASE_NAME + "-*.txt", so a
        # dash here would make "dccore-size.txt" match "DCCore-*.txt" on a
        # case-insensitive filesystem and sort after the dated list - the
        # daemon would serve its own size file as the master list.
        self.assertEqual(config.LIST_SIZE_FILE, "dccore.size.txt")
        self.assertEqual(config.LIST_RAWBYTES_FILE, "dccore.rawbytes.txt")
        for name in (config.LIST_SIZE_FILE, config.LIST_RAWBYTES_FILE):
            self.assertFalse(name.lower().startswith(config.LIST_BASE_NAME.lower() + "-"),
                             f"{name} would be picked up by the master-list glob")


class ReadingBrokenSideFiles(DCCoreTestCase):
    """One clipped byte count must not cost the caller everything else."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.LIST_BASE_NAME = "DCCore"
        self.write_master_list(42)

    def write_master_list(self, count):
        path = os.path.join(self.tree.lists, f"{config.LIST_BASE_NAME}-2026-08-25.txt")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("List of 42 Files (1.5GB) generated on Aug 25th\n")
            handle.write("header two\n")
            for i in range(count):
                handle.write(f"!{config.LIST_BASE_NAME} track {i}.flac  ::INFO:: 3.0MB\n")
        return path

    def write_side(self, size_text, raw_text):
        with open(list_mod.size_file_path(), "w", encoding="utf-8", newline="\n") as f:
            f.write(size_text)
        with open(list_mod.rawbytes_file_path(), "w", encoding="utf-8", newline="\n") as f:
            f.write(raw_text)

    def test_good_side_files_read_back(self):
        self.write_side("1.5GB", "1610612736")
        count, date_str, size_str, raw_bytes = list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(count, 42)
        self.assertEqual(size_str, "1.5GB")
        self.assertEqual(raw_bytes, 1610612736)
        self.assertNotEqual(date_str, "Error")

    def test_a_truncated_rawbytes_file_costs_only_the_byte_count(self):
        """The regression: int("") raised and collapsed the whole tuple."""
        self.write_side("1.5GB", "")
        count, date_str, size_str, raw_bytes = list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(count, 42, "the file count comes from the master list and was fine")
        self.assertNotEqual(date_str, "Error", "the list date was fine too")
        self.assertEqual(size_str, "1.5GB")
        self.assertEqual(raw_bytes, 0)

    def test_a_garbage_rawbytes_file_is_survivable(self):
        for junk in ("not a number", "12.5", "\x00\x00", "1610612736 extra"):
            with self.subTest(junk=junk):
                self.write_side("1.5GB", junk)
                count, date_str, _, raw_bytes = \
                    list_mod.get_file_count_date_size_and_raw_bytes()
                self.assertEqual(count, 42)
                self.assertNotEqual(date_str, "Error")
                self.assertEqual(raw_bytes, 0)

    def test_an_empty_size_file_falls_back_rather_than_printing_nothing(self):
        self.write_side("", "1610612736")
        _, _, size_str, raw_bytes = list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(size_str, "0B", "an empty string in the advert reads as a bug")
        self.assertEqual(raw_bytes, 1610612736)

    def test_missing_side_files_are_not_an_error(self):
        """A fresh install has neither, and must still report its file count."""
        count, date_str, size_str, raw_bytes = list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(count, 42)
        self.assertNotEqual(date_str, "Error")
        self.assertEqual((size_str, raw_bytes), ("0B", 0))


class GenerationPublishesStatsWithTheList(DCCoreTestCase):
    """The side files must move with the lists, not ahead of them."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.FILE_DIRECTORY = self.tree.music
        config.LIST_BASE_NAME = "DCCore"
        config.NICKNAME = "DCCore"

    def side_files(self):
        size, raw = "<missing>", "<missing>"
        if os.path.exists(list_mod.size_file_path()):
            size = open(list_mod.size_file_path(), encoding="utf-8").read().strip()
        if os.path.exists(list_mod.rawbytes_file_path()):
            raw = open(list_mod.rawbytes_file_path(), encoding="utf-8").read().strip()
        return size, raw

    def test_a_successful_run_publishes_both_side_files(self):
        self.assertTrue(update_list.generate_master_list())
        size, raw = self.side_files()
        self.assertNotEqual(size, "<missing>")
        self.assertNotEqual(raw, "<missing>")
        self.assertGreater(int(raw), 0, "TempTree writes two 4096-byte tracks")

    def test_a_failed_scan_leaves_the_previous_stats_alone(self):
        """The bug. The guard kept the list; the side files were already gone.

        First a good run, then point the scanner at an empty directory - which is
        what an NFS mount that has gone away looks like from here. The previous
        index is kept, and the published size must be kept with it.
        """
        self.assertTrue(update_list.generate_master_list())
        good_size, good_raw = self.side_files()
        self.assertNotEqual(good_size, "<missing>")

        empty = os.path.join(self.tree.root, "vanished")
        os.makedirs(empty, exist_ok=True)
        config.FILE_DIRECTORY = empty

        self.assertFalse(update_list.generate_master_list(),
                         "a scan finding nothing next to a good index must refuse")

        self.assertEqual(self.side_files(), (good_size, good_raw),
                         "keeping the list but zeroing its published size is half a rollback")

    def test_the_advert_numbers_stay_consistent_after_a_failed_scan(self):
        """What the operator actually sees: count and size must agree."""
        self.assertTrue(update_list.generate_master_list())
        before = list_mod.get_file_count_date_size_and_raw_bytes()

        empty = os.path.join(self.tree.root, "vanished2")
        os.makedirs(empty, exist_ok=True)
        config.FILE_DIRECTORY = empty
        update_list.generate_master_list()

        after = list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(before, after,
                         "a failed scan must be invisible to everyone reading the stats")
        self.assertGreater(after[0], 0)
        self.assertNotEqual(after[2], "0B")

    def test_a_first_run_on_an_empty_library_still_publishes(self):
        """The guard only refuses when there is a working index to lose."""
        empty = os.path.join(self.tree.root, "brand-new")
        os.makedirs(empty, exist_ok=True)
        config.FILE_DIRECTORY = empty
        self.assertTrue(update_list.generate_master_list())
        size, raw = self.side_files()
        self.assertEqual(raw, "0")


class SideFilesAreWrittenAtomically(DCCoreTestCase):
    """A plain open(..., "w") truncates before it writes."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.FILE_DIRECTORY = self.tree.music
        config.LIST_BASE_NAME = "DCCore"

    def test_generation_uses_the_atomic_writer(self):
        """That both side files are PUBLISHED through db._atomic_write, not
        that update_list.py contains that call as text.

        The previous version was four substring checks against the source. Two
        asserted the call appears; two asserted a truncating open() does not.
        Putting `if False:` in front of the real calls satisfied all four, and
        so would moving them into a branch that never runs.

        The test below it already drives the same publish to prove a failed
        write leaves the old values alone - so the machinery to check this
        properly was sitting directly underneath the source scan the whole
        time.
        """
        import db

        real = db._atomic_write
        written = []

        def recorder(path, text):
            written.append(os.path.basename(path))
            return real(path, text)

        db._atomic_write = recorder
        try:
            self.assertTrue(update_list.generate_master_list())
        finally:
            db._atomic_write = real

        self.assertIn(config.LIST_SIZE_FILE, written,
                      f"the size file was written some other way: {written}")
        self.assertIn(config.LIST_RAWBYTES_FILE, written,
                      f"the raw byte count was written some other way: {written}")

    def test_the_side_files_are_never_left_readable_and_empty(self):
        """What the two assertFalse(open(...)) source checks were reaching for.

        A truncate-then-write leaves a zero-length file readable for the
        instant between the two, and the advert reads these on a timer: it
        would publish a size of nothing. Asserted by watching the file rather
        than by looking for the shape of a call that would cause it.
        """
        self.assertTrue(update_list.generate_master_list())

        for path in (list_mod.size_file_path(), list_mod.rawbytes_file_path()):
            with self.subTest(path=os.path.basename(path)):
                self.assertTrue(os.path.getsize(path) > 0)

        # Rebuild over the top: the publish replaces rather than truncates, so
        # there is no moment at which the file exists and is empty.
        sizes = []
        real_open = io.open

        def watching_open(target, *args, **kwargs):
            if isinstance(target, str) and os.path.basename(target) in (
                    config.LIST_SIZE_FILE, config.LIST_RAWBYTES_FILE):
                if os.path.exists(target):
                    sizes.append(os.path.getsize(target))
            return real_open(target, *args, **kwargs)

        io.open = watching_open
        try:
            self.assertTrue(update_list.generate_master_list())
        finally:
            io.open = real_open

        self.assertNotIn(0, sizes,
                         "a side file was observed existing and empty")

    def test_an_interrupted_publish_leaves_the_previous_values(self):
        """Behaviour, not source text: make the write fail and read back."""
        import db
        self.assertTrue(update_list.generate_master_list())
        good = self.read_side()

        real = db._atomic_write
        calls = []

        def fail_on_side_files(path, text):
            calls.append(path)
            if os.path.basename(path) in (config.LIST_SIZE_FILE, config.LIST_RAWBYTES_FILE):
                raise OSError(28, "No space left on device")
            return real(path, text)

        db._atomic_write = fail_on_side_files
        try:
            update_list.generate_master_list()
        except OSError:
            pass
        finally:
            db._atomic_write = real

        self.assertTrue(calls, "the publish path must go through the atomic writer")
        self.assertEqual(self.read_side(), good,
                         "a failed publish must not destroy the values already on disk")

    def read_side(self):
        size = open(list_mod.size_file_path(), encoding="utf-8").read()
        raw = open(list_mod.rawbytes_file_path(), encoding="utf-8").read()
        return size, raw


if __name__ == "__main__":
    unittest.main()
