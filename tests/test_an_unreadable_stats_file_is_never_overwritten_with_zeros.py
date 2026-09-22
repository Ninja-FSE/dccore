"""A stats.txt that cannot be read for a moment must not be replaced with
zeros (#626).

_load_advanced_stats_unlocked() used to catch any error from open()/read()
and return the all-zero row. For a display that is harmless. For the writers
it was not: update_stats_on_complete() incremented the zeros and atomically
wrote them over the real file, and unlike the malformed-column path it kept no
.corrupt copy. One share-deny lock from an AV or backup tool at the instant a
transfer completed - the same class of interference replace_with_retry()
exists for - cost the lifetime totals, which nothing recomputes.

The file was FINE; it just could not be opened right then. So the writers now
raise and write nothing, their callers (dcc.py, irc.py) already report and
carry on, and the read-only entry points keep answering with zeros for that
one refresh. These tests drive the real functions against a real file, with
open() refused exactly once for that path.
"""
import contextlib
import datetime
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import support  # noqa: E402

import defaults as config  # noqa: E402
import db  # noqa: E402


TODAY = datetime.datetime.now().strftime("%Y-%m-%d")
YESTERDAY = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
LIFETIME = f"5000 123456789 3 4 5 6 {TODAY}"


class UnreadableStatsFile(support.DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(STATS_FILE=os.path.join(self.tree.root, "stats.txt"))
        sink = contextlib.redirect_stdout(io.StringIO())
        sink.__enter__()
        self.addCleanup(sink.__exit__, None, None, None)

    def write(self, body):
        with io.open(config.STATS_FILE, "w", encoding="utf-8") as handle:
            handle.write(body)

    def on_disk(self):
        with io.open(config.STATS_FILE, encoding="utf-8") as handle:
            return handle.read()

    def refusing_open(self, times=1):
        """Patch the open() db.py sees so the stats file is refused `times`
        times, the way a share-deny lock refuses it, then works again."""
        real_open = open
        refusals = {"left": times}

        def failing_open(file, *args, **kwargs):
            if os.fspath(file) == config.STATS_FILE and refusals["left"] > 0:
                refusals["left"] -= 1
                raise PermissionError(13, "Simulated share-deny lock", str(file))
            return real_open(file, *args, **kwargs)

        return mock.patch.object(db, "open", failing_open, create=True)

    # ---------------------------------------------------------------- writers

    def test_a_completion_during_the_failure_does_not_overwrite_the_totals(self):
        """The defect: one refused read at the end of one transfer, and the
        file held '1 1000 0 0 1 1000 <today>' - years of totals gone."""
        self.write(LIFETIME)

        with self.refusing_open():
            with self.assertRaises(OSError):
                db.update_stats_on_complete(1000)

        self.assertEqual(self.on_disk(), LIFETIME)

    def test_the_file_is_not_preserved_as_corrupt_either(self):
        """Nothing was wrong with the file, so it must not be renamed away as
        damaged - that would ALSO leave the writers starting from zero."""
        self.write(LIFETIME)

        with self.refusing_open():
            with self.assertRaises(OSError):
                db.update_stats_on_complete(1000)

        self.assertFalse(os.path.exists(config.STATS_FILE + ".corrupt"))
        self.assertTrue(os.path.exists(config.STATS_FILE))

    def test_the_next_completion_counts_on_top_of_the_real_totals(self):
        """End to end: the failure clears, and the next transfer lands on the
        preserved totals rather than on a fresh zero row."""
        self.write(LIFETIME)

        with self.refusing_open():
            with self.assertRaises(OSError):
                db.update_stats_on_complete(1000)
        stats = db.update_stats_on_complete(1000)

        self.assertEqual(stats[:2], [5001, 123456789 + 1000])
        self.assertEqual(self.on_disk().split()[:2], ["5001", str(123456789 + 1000)])

    def test_the_midnight_rotation_does_not_write_over_an_unreadable_file(self):
        """check_and_rotate_day() is the other writer. A stale date with a
        refused read used to rotate a zero row and save THAT."""
        self.write(f"5000 123456789 3 4 5 6 {YESTERDAY}")

        with self.refusing_open():
            with self.assertRaises(OSError):
                db.check_and_rotate_day()

        self.assertEqual(self.on_disk(), f"5000 123456789 3 4 5 6 {YESTERDAY}")

    # ---------------------------------------------------------------- readers

    def test_a_display_still_gets_a_row_and_writes_nothing(self):
        """The dashboard, the advert and the -stats reply answer with zeros for
        that one refresh rather than raising - and leave the file alone."""
        self.write(LIFETIME)

        with self.refusing_open(times=2):
            plain = db.load_advanced_stats()
            rolled = db.load_advanced_stats_rolled()

        self.assertEqual(plain[:6], [0, 0, 0, 0, 0, 0])
        self.assertEqual(rolled[:6], [0, 0, 0, 0, 0, 0])
        self.assertEqual(self.on_disk(), LIFETIME)

    def test_a_display_says_why_it_shows_zeros(self):
        self.write(LIFETIME)
        buffer = io.StringIO()

        with self.refusing_open(), contextlib.redirect_stdout(buffer):
            db.load_advanced_stats()

        self.assertIn("Could not read stats.txt", buffer.getvalue())

    def test_a_missing_file_is_still_a_fresh_start_for_a_writer(self):
        """The control: no file at all is the first run, not a failure, and a
        completion creates it."""
        self.assertFalse(os.path.exists(config.STATS_FILE))

        stats = db.update_stats_on_complete(1000)

        self.assertEqual(stats[:2], [1, 1000])
        self.assertEqual(self.on_disk().split()[:2], ["1", "1000"])


if __name__ == "__main__":
    unittest.main()
