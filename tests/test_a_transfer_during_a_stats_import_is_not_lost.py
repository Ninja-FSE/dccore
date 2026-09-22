"""The stats import read and wrote the row in two separate lock
acquisitions (audit L26, #690).

apply_stats_import() loaded the 7-column row with db.load_advanced_stats(),
set columns 0 and 1, and wrote it with db.save_advanced_stats() - two
_disk_lock acquisitions. A transfer completing in the gap
(db.update_stats_on_complete(), on the send's own thread) had its +1 file
and +bytes on Today and Total discarded by the import's stale write - the
lost update db.py's own header describes for the old dcc.py code - and a
day rotation in the gap was undone. The import still answered 200.

db.set_lifetime_totals() reads, modifies and writes under one acquisition;
the import calls it. The race is forced here, not bet on: the completion
runs on another thread that waits on the lock the import holds, and the
old shape is modelled with the audit's own trick of a completion between
the two calls.
"""

import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import webserver  # noqa: E402

from tests import test_stats_import as stats  # noqa: E402


class _ReleasesTheLockLate:
    """A stand-in for _load_advanced_stats_unlocked that, on the import's
    OWN read (its second call - the first is the `before` snapshot, as the
    audit's probe noted), lets a completion thread start and gives it a
    moment to reach the lock - so the completion is provably waiting on the
    import's single acquisition, and lands after it."""

    def __init__(self, real, start_completion):
        self.real, self.start_completion, self.calls = real, start_completion, 0

    def __call__(self):
        row = self.real()
        self.calls += 1
        if self.calls == 2:
            self.start_completion()
        return row


class TheImportHoldsTheLockOnce(stats.ImportCase):

    def setUp(self):
        super().setUp()
        self.given_existing(files=100, total_bytes=1000)
        # A real Today column, as the audit seeded it.
        db.save_advanced_stats([100, 1000, 0, 0, 5, 50, db._default_stats_row()[6]])

    def test_a_transfer_completing_during_the_import_is_in_the_row_that_lands(self):
        completed = threading.Event()
        started = threading.Event()

        def complete_one():
            started.set()
            db.update_stats_on_complete(7)   # blocks on _disk_lock until the import lets go
            completed.set()

        def start_completion():
            threading.Thread(target=complete_one, daemon=True).start()
            started.wait(2)
            # The completion thread is now parked on the lock the import
            # holds (it cannot have finished: we hold the lock); give the
            # scheduler a moment so the property is the ORDER, not luck.
            completed.wait(0.2)
            self.assertFalse(completed.is_set(), "the completion got in before the import's write")

        real = db._load_advanced_stats_unlocked
        db._load_advanced_stats_unlocked = _ReleasesTheLockLate(real, start_completion)
        self.addCleanup(setattr, db, "_load_advanced_stats_unlocked", real)

        status, _result = webserver.apply_stats_import({"total_files": 5000, "total_bytes": 999999})
        self.assertTrue(completed.wait(5), "the completion never got the lock")

        self.assertEqual(status, 200)
        row = db.load_advanced_stats()
        self.assertEqual(row[:2], [5001, 1000006], "the import's totals, plus the transfer that landed after it")
        self.assertEqual(row[4:6], [6, 57], "Today kept the transfer")

    def test_the_day_columns_and_the_date_are_left_alone(self):
        webserver.apply_stats_import({"total_files": 5000})

        row = db.load_advanced_stats()
        self.assertEqual(row[:6], [5000, 1000, 0, 0, 5, 50])
        self.assertEqual(row[6], db._default_stats_row()[6])

    def test_the_old_shape_lost_the_transfer(self):
        """The audit's probe, against the old two-call shape modelled here:
        a completion between the load and the save is overwritten. This is
        what set_lifetime_totals() exists to make impossible."""
        row = list(db.load_advanced_stats())
        db.update_stats_on_complete(7)          # a transfer completes in the gap
        row[0], row[1] = 5000, 999999
        db.save_advanced_stats(row)             # the stale row lands

        self.assertEqual(db.load_advanced_stats()[:6], [5000, 999999, 0, 0, 5, 50])


class TheHelperOnItsOwn(stats.ImportCase):

    def test_it_sets_only_what_it_is_given(self):
        db.save_advanced_stats([1, 2, 3, 4, 5, 6, "2026-01-01"])

        self.assertEqual(db.set_lifetime_totals(total_bytes=99)[:6], [1, 99, 3, 4, 5, 6])
        self.assertEqual(db.set_lifetime_totals(total_files=7)[:6], [7, 99, 3, 4, 5, 6])

    def test_a_missing_file_starts_from_the_default_row(self):
        os.remove(os.path.join(self.dir, "stats.txt")) if os.path.exists(os.path.join(self.dir, "stats.txt")) else None

        row = db.set_lifetime_totals(total_files=3, total_bytes=4)

        self.assertEqual(row[:6], [3, 4, 0, 0, 0, 0])
        self.assertEqual(db.load_advanced_stats()[:2], [3, 4])

    def test_a_totals_write_that_did_not_land_is_still_reported_as_a_failure(self):
        """The totals are judged by the row the locked write produced; a
        write that raised produces none, and the import says so - the
        counterpart of test_stats_import's speed-record case."""
        real = db._save_advanced_stats_unlocked

        def refuses(_row):
            raise OSError("disk full")
        db._save_advanced_stats_unlocked = refuses
        self.addCleanup(setattr, db, "_save_advanced_stats_unlocked", real)
        import contextlib
        import io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            status, result = webserver.apply_stats_import({"total_files": 45902})

        self.assertEqual(status, 500)
        self.assertEqual(result["failed"], ["total_files"])

    def test_the_import_goes_through_it(self):
        import io
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            body = handle.read()
        start = body.index("def apply_stats_import(")
        function = body[start:body.index("\ndef ", start + 1)]

        self.assertIn("db.set_lifetime_totals(", function)
        self.assertNotIn("db.save_advanced_stats(", function)


if __name__ == "__main__":
    unittest.main()
