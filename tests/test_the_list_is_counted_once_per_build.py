"""The file count is computed once per list build, not once per caller.

Found on a live install with 5.4 million files - a 460 MB list. The number of
files the bot shares is asked for constantly: every advert cycle, every Stats
page load, every -que from somebody with nothing queued, the admin console's
status. It was answered by reading every published list end to end and
counting the lines that start with "!": three seconds warm on that install,
considerably more cold after a restart, paid again by every caller, for a
number that cannot change between one !update and the next. The operator saw
the Stats page sit on dashes and assumed it was broken.

The cache key is each list file's path, mtime and size. update_list.py
publishes with os.replace(), which gives the new list a new mtime and (almost
always) a new size, so the first call after a rebuild recounts once and every
call until the next rebuild is free. Nothing is invalidated by hand: the file
on disk is the truth, and the key is the file on disk.
"""

import io
import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class CountingBase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.LIST_BASE_NAME = "DCCore"
        list_mod._count_cache.clear()
        self.reads = []
        self._real_open = open

    def write_list(self, count, name="DCCore-2026-09-15.txt", bump_mtime=True):
        path = os.path.join(self.tree.lists, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"List of {count} Files generated on Sep 15th\n")
            handle.write("=== Some Folder ===\n")
            for i in range(count):
                handle.write(f"!DCCore track {i}.flac  ::INFO:: 3.0MB\n")
        if bump_mtime:
            # Two writes inside one clock tick would share an mtime; a rebuild
            # in the real daemon is never that fast, so make the test honest.
            stamp = time.time() + len(self.reads) + 1
            os.utime(path, (stamp, stamp))
        return path

    def counting_reads(self):
        """Swap in an open() that records every list file actually read."""
        import builtins
        real = builtins.open
        reads = self.reads

        def spy(path, *args, **kwargs):
            if str(path).endswith(".txt") and self.tree.lists in str(path):
                reads.append(str(path))
            return real(path, *args, **kwargs)

        builtins.open = spy
        self.addCleanup(setattr, builtins, "open", real)


class TheAnswerIsUnchanged(CountingBase):
    """A cache that changes the number is not a cache."""

    def test_counts_only_request_lines(self):
        path = self.write_list(7)
        self.assertEqual(list_mod.count_request_lines([path]), 7)

    def test_the_full_tuple_is_what_it_always_was(self):
        self.write_list(42)
        count, date_str, size_str, raw_bytes = list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(count, 42)
        self.assertNotEqual(date_str, "Error")

    def test_two_lists_are_both_counted(self):
        a = self.write_list(3, name="DCCore-2026-09-15.txt")
        b = self.write_list(4, name="DCCore-VIDEO-2026-09-15.txt")
        self.assertEqual(list_mod.count_request_lines([a, b]), 7)


class TheListIsReadOnce(CountingBase):

    def test_a_second_call_does_not_reopen_the_file(self):
        """The whole point. Three seconds per call on the live install."""
        path = self.write_list(5)
        self.counting_reads()
        list_mod.count_request_lines([path])
        list_mod.count_request_lines([path])
        list_mod.count_request_lines([path])
        self.assertEqual(len(self.reads), 1,
                         f"the list was read {len(self.reads)} times for three calls")

    def test_every_caller_shares_it(self):
        """Advert, Stats page, -que and the console all go through the same
        function, so one read serves all of them."""
        self.write_list(5)
        self.counting_reads()
        for _ in range(4):
            list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(len(self.reads), 1)

    def test_concurrent_callers_do_not_each_pay_the_read(self):
        """Two threads arriving together must not both walk 460 MB."""
        path = self.write_list(5)
        self.counting_reads()
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            list_mod.count_request_lines([path]))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, [5] * 6)
        self.assertEqual(len(self.reads), 1)


class ARebuildIsNoticed(CountingBase):
    """The cache must never outlive the list it counted."""

    def test_a_new_list_is_recounted(self):
        path = self.write_list(5)
        self.assertEqual(list_mod.count_request_lines([path]), 5)
        self.write_list(9)          # same name, new content, new mtime
        self.assertEqual(list_mod.count_request_lines([path]), 9)

    def test_a_list_that_changes_size_but_not_mtime_is_recounted(self):
        """Belt and braces: the key is mtime AND size, so either moving
        catches it."""
        path = self.write_list(5)
        self.assertEqual(list_mod.count_request_lines([path]), 5)
        stamp = os.stat(path).st_mtime
        self.write_list(6, bump_mtime=False)
        os.utime(path, (stamp, stamp))    # pin the mtime back
        self.assertEqual(list_mod.count_request_lines([path]), 6)

    def test_a_rebuild_of_the_same_byte_size_is_recounted(self):
        """The other half of the key. One long filename gone and two short
        ones added leaves the file the same size with a different count;
        only the mtime moves, and it has to be enough."""
        path = os.path.join(self.tree.lists, "DCCore-2026-09-15.txt")
        first = "!alpha\n!beta\n!gamma\n!delta\n"          # 4 request lines
        # 3 request lines plus a separator padded so the byte count matches.
        second_lines = "!a\n!b\n!c\n"
        padding = "=" * (len(first) - len(second_lines) - 1) + "\n"
        second = second_lines + padding
        self.assertEqual(len(first), len(second), "the test needs identical sizes")

        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(first)
        os.utime(path, (1_700_000_000, 1_700_000_000))
        self.assertEqual(list_mod.count_request_lines([path]), 4)

        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(second)
        os.utime(path, (1_700_000_100, 1_700_000_100))
        self.assertEqual(os.stat(path).st_size, len(first))
        self.assertEqual(list_mod.count_request_lines([path]), 3,
                         "a same-size rebuild was served the old count")

    def test_a_different_set_of_paths_is_a_different_answer(self):
        a = self.write_list(3, name="DCCore-2026-09-15.txt")
        b = self.write_list(4, name="DCCore-VIDEO-2026-09-15.txt")
        self.assertEqual(list_mod.count_request_lines([a]), 3)
        self.assertEqual(list_mod.count_request_lines([a, b]), 7)
        self.assertEqual(list_mod.count_request_lines([a]), 3)

    def test_a_vanished_list_is_not_served_from_memory(self):
        path = self.write_list(5)
        self.assertEqual(list_mod.count_request_lines([path]), 5)
        os.remove(path)
        self.assertEqual(list_mod.count_request_lines([path]), 0)


class AShortCountIsNeverKept(CountingBase):
    """#433 again: a list that stat()s fine but will not open.

    An AV scanner holding the VIDEO list open on Windows leaves the file's
    mtime and size exactly as they were, so the cache key does not move when
    the scanner lets go. The uncached code retried on the next call and got
    the full count back. Caching the short count would serve it until the
    next !update.
    """

    def test_an_unreadable_list_costs_only_its_own_lines(self):
        a = self.write_list(3, name="DCCore-2026-09-15.txt")
        b = self.write_list(4, name="DCCore-VIDEO-2026-09-15.txt")
        self.refuse_to_open(b)
        self.assertEqual(list_mod.count_request_lines([a, b]), 3)

    def test_the_short_count_is_recounted_once_the_file_opens_again(self):
        a = self.write_list(3, name="DCCore-2026-09-15.txt")
        b = self.write_list(4, name="DCCore-VIDEO-2026-09-15.txt")
        restore = self.refuse_to_open(b)
        self.assertEqual(list_mod.count_request_lines([a, b]), 3)
        restore()
        self.assertEqual(list_mod.count_request_lines([a, b]), 7,
                         "the short count was cached under an unchanged signature")

    def refuse_to_open(self, blocked):
        import builtins
        real = builtins.open

        def spy(path, *args, **kwargs):
            if str(path) == blocked:
                raise OSError(13, "held open by a scanner")
            return real(path, *args, **kwargs)

        builtins.open = spy

        def restore():
            builtins.open = real
        self.addCleanup(restore)
        return restore


class ItIsWiredIn(unittest.TestCase):

    def test_the_lock_is_runtime_dot_pys_not_list_dot_pys(self):
        """list.py is reloaded by !rehash. A Lock() constructed there is a new
        object after every reload while a caller mid-count still holds the old
        one - two threads walking 460 MB at once, the exact cost the cache
        removes. tests/test_no_reloaded_module_owns_a_lock.py caught the first
        draft doing precisely that."""
        import runtime
        self.assertIs(list_mod._count_lock, runtime.list_count_lock)


    def test_the_tuple_function_counts_through_the_cache(self):
        with io.open(os.path.join(REPO_ROOT, "list.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def get_file_count_date_size_and_raw_bytes("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("count_request_lines(all_list_paths(name))", body)
        self.assertNotIn('startswith("!")', body,
                         "the counting loop must live in one place, the cached one")


if __name__ == "__main__":
    unittest.main()
