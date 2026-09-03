"""stats.txt must not lose counts when transfers finish at the same time.

WHY THESE TESTS FORCE THE INTERLEAVING

The counters in stats.txt are only ever derived from their own previous value -
nothing recomputes them from the library or from a log - so a lost update is
permanent. Up to MAX_DCC_SLOTS transfers finish concurrently, each in its own
thread, and check_and_rotate_day() runs from the IRC read loop on every channel
message.

The old shape was a load, a mutate, and a save as three separate calls, with
_disk_lock held only inside the save. Two completions overlapping in that window
both read the same row and the second write discarded the first one's increment.

A test that just spawned threads and hoped would be flaky in both directions:
it could pass on a fixed build by luck and pass on a broken one by luck too. So
these tests WIDEN the window deliberately, by making the read slow, and then
assert an exact arithmetic result. On the locked implementation the slow read
happens inside the critical section and costs only wall-clock; on an unlocked
one it guarantees the overlap. Mutation-checked: reverting to load/mutate/save
makes test_no_increment_is_lost fail every run, not one in ten.
"""
import contextlib
import datetime
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import support  # noqa: E402

import defaults as config  # noqa: E402
import db  # noqa: E402


class StatsFileTestCase(support.DCCoreTestCase):
    """A real stats.txt in a temp dir, cleaned up afterwards."""

    def setUp(self):
        super().setUp()
        # Swallow the daemon's log output for the duration of each test.
        #
        # The code under test prints Swedish log lines, and on a console whose
        # code page cannot encode them (cp1253, cp1251, cp932, ascii) print()
        # raises UnicodeEncodeError - a separate, already-tracked defect that
        # has nothing to do with stats locking, but would fail these tests for
        # the wrong reason. Captured rather than reconfigured on purpose:
        # reconfiguring the real sys.stdout would apply process-wide and hide
        # that defect in OTHER test modules too, which is not this file's call
        # to make.
        sink = contextlib.redirect_stdout(io.StringIO())
        sink.__enter__()
        self.addCleanup(sink.__exit__, None, None, None)

        self._dir = tempfile.mkdtemp(prefix="dccore-stats-")
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.stats_path = os.path.join(self._dir, "stats.txt")
        config.STATS_FILE = self.stats_path
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        db.save_advanced_stats([0, 0, 0, 0, 0, 0, today])


class TheLockCoversTheWholeReadModifyWrite(StatsFileTestCase):

    def test_no_increment_is_lost(self):
        """The defect: concurrent completions overwrote each other's counts."""
        THREADS, PER_THREAD, SIZE = 4, 25, 1000

        # Widen the read window so an unlocked implementation is GUARANTEED to
        # interleave rather than merely likely to. Under the fix this runs
        # inside the critical section, so the arithmetic is unaffected.
        real_load = db._load_advanced_stats_unlocked

        def slow_load():
            stats = real_load()
            time.sleep(0.001)
            return stats

        db._load_advanced_stats_unlocked = slow_load
        self.addCleanup(setattr, db, "_load_advanced_stats_unlocked", real_load)

        errors = []

        def worker():
            try:
                for _ in range(PER_THREAD):
                    db.update_stats_on_complete(SIZE)
            except Exception as err:          # pragma: no cover
                errors.append(err)

        threads = [threading.Thread(target=worker) for _ in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertFalse([t for t in threads if t.is_alive()], "a worker hung")

        expected_files = THREADS * PER_THREAD
        stats = db.load_advanced_stats()
        self.assertEqual(stats[0], expected_files, "total files lost an increment")
        self.assertEqual(stats[1], expected_files * SIZE, "total bytes lost an increment")
        self.assertEqual(stats[4], expected_files, "today files lost an increment")
        self.assertEqual(stats[5], expected_files * SIZE, "today bytes lost an increment")

    def test_the_row_on_disk_is_never_torn(self):
        """Every reader must see a complete 7-column row, never a partial one."""
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                row = db.load_advanced_stats()
                if not (isinstance(row, list) and len(row) == 7):
                    bad.append(row)

        r = threading.Thread(target=reader, daemon=True)
        r.start()
        for _ in range(60):
            db.update_stats_on_complete(512)
        stop.set()
        r.join(timeout=10)

        self.assertEqual(bad, [], "a reader saw a torn row")


class MidnightRotationIsAtomicToo(StatsFileTestCase):

    def test_a_transfer_across_midnight_does_not_resurrect_yesterday(self):
        """The nastier half of the race.

        A transfer thread that loaded BEFORE the rotation and saved AFTER it
        used to write the old date back together with un-rotated counters, so
        the next check_and_rotate_day() rotated a second time and yesterday's
        totals collapsed to whatever that one transfer had carried.
        """
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        db.save_advanced_stats([100, 100000, 0, 0, 40, 40000, yesterday])

        # First completion of the new day: rotates AND counts, in one step.
        db.update_stats_on_complete(500)

        stats = db.load_advanced_stats()
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(stats[6], today, "the date did not roll over")
        self.assertEqual(stats[2], 40, "yesterday's file count was lost")
        self.assertEqual(stats[3], 40000, "yesterday's byte count was lost")
        self.assertEqual(stats[4], 1, "today should hold only the new transfer")
        self.assertEqual(stats[5], 500)
        self.assertEqual(stats[0], 101, "the lifetime total must still climb")

    def test_rotating_twice_does_not_wipe_yesterday(self):
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        db.save_advanced_stats([10, 1000, 0, 0, 7, 700, yesterday])

        first = db.check_and_rotate_day()
        second = db.check_and_rotate_day()

        self.assertEqual(first[2], 7)
        self.assertEqual(second[2], 7, "the second call rotated again and lost yesterday")
        self.assertEqual(second[3], 700)


class NoPublicEntryPointDeadlocks(StatsFileTestCase):
    """_disk_lock is a plain Lock, not an RLock.

    If any of these ever calls another public entry point while already holding
    it, the daemon hangs instead of crashing - the worst failure mode there is,
    because it looks like a network stall. These run each one in a thread with a
    timeout so a regression shows up as a failure rather than a hung suite.
    """

    def _must_finish(self, call, label):
        done = threading.Event()

        def run():
            try:
                call()
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(timeout=10), f"{label} deadlocked on _disk_lock")

    def test_load_does_not_deadlock(self):
        self._must_finish(db.load_advanced_stats, "load_advanced_stats")

    def test_save_does_not_deadlock(self):
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        self._must_finish(lambda: db.save_advanced_stats([1, 2, 3, 4, 5, 6, today]),
                          "save_advanced_stats")

    def test_rotate_does_not_deadlock(self):
        self._must_finish(db.check_and_rotate_day, "check_and_rotate_day")

    def test_update_does_not_deadlock(self):
        self._must_finish(lambda: db.update_stats_on_complete(123),
                          "update_stats_on_complete")

    def test_rotate_then_update_does_not_deadlock(self):
        def both():
            db.check_and_rotate_day()
            db.update_stats_on_complete(1)
        self._must_finish(both, "check_and_rotate_day + update_stats_on_complete")


class TheAwkwardCallersStillWork(StatsFileTestCase):
    """update_stats_on_complete has been fed a list, a dict and a decimal string
    in the field. That coercion moved into a helper and must not have changed."""

    def test_a_plain_integer(self):
        self.assertEqual(db.update_stats_on_complete(2048)[1], 2048)

    def test_a_list(self):
        self.assertEqual(db.update_stats_on_complete([4096])[1], 4096)

    def test_an_empty_list_counts_as_zero(self):
        stats = db.update_stats_on_complete([])
        self.assertEqual(stats[1], 0)
        self.assertEqual(stats[0], 1, "the file itself must still be counted")

    def test_a_dict_with_bytes(self):
        self.assertEqual(db.update_stats_on_complete({"bytes": 700})[1], 700)

    def test_a_dict_with_size(self):
        self.assertEqual(db.update_stats_on_complete({"size": 900})[1], 900)

    def test_a_decimal_string(self):
        self.assertEqual(db.update_stats_on_complete("1234.0")[1], 1234)

    def test_unparseable_falls_back_to_zero_without_raising(self):
        stats = db.update_stats_on_complete("not a number")
        self.assertEqual(stats[1], 0)
        self.assertEqual(stats[0], 1, "the transfer still happened")

    def test_the_counters_accumulate_across_calls(self):
        db.update_stats_on_complete(10)
        db.update_stats_on_complete(20)
        stats = db.update_stats_on_complete(30)
        self.assertEqual(stats[0], 3)
        self.assertEqual(stats[1], 60)


if __name__ == "__main__":
    unittest.main()
