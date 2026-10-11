"""#1273: a fetch-history write that failed was never tried again.

dcc_fetch remembered a snapshot as written BEFORE db.save_fetch_history() ran,
and the save printed its error and returned as if nothing had happened. One
failed write - a Windows sharing violation past the retries, a full disk - so
made every later tick find the rows "unchanged": finished and pending fetches,
and a dashboard delete, never reached the file, and a restart lost them or
brought deleted rows back. The bot registry had the same bug fixed in #691.

The save now says whether it wrote, and the snapshot is remembered only then.
"""

import os
import time
import unittest

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase  # noqa: E402

import db  # noqa: E402
import dcc_fetch  # noqa: E402


class TheFetchHistory(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        queue = dcc_fetch._ensure_fetch_queue()
        before = dict(queue)
        self.addCleanup(lambda: (queue.clear(), queue.update(before)))
        queue.clear()
        queue["r1"] = {"bot": "alfabot", "filename": "Song.flac", "state": "pending",
                       "requested_at": time.time()}
        self.attempts = 0
        real_write = db._atomic_write

        def write(path, text, *args, **kwargs):
            if path == db.FETCH_HISTORY_FILE:
                self.attempts += 1
                if self.attempts == 1:
                    raise PermissionError(13, "The process cannot access the file", path)
            return real_write(path, text, *args, **kwargs)

        self.addCleanup(setattr, db, "_atomic_write", real_write)
        db._atomic_write = write

    def test_the_next_tick_writes_what_the_failed_one_did_not(self):
        dcc_fetch.persist_fetch_history()
        self.assertEqual(self.attempts, 1)
        self.assertFalse(os.path.exists(db.FETCH_HISTORY_FILE))

        dcc_fetch.persist_fetch_history()
        self.assertEqual(self.attempts, 2, "the unchanged rows must be written again after a failure")
        self.assertEqual(sorted(db.load_fetch_history()), ["r1"])

    def test_once_written_an_unchanged_tick_writes_nothing(self):
        dcc_fetch.persist_fetch_history()
        dcc_fetch.persist_fetch_history()
        dcc_fetch.persist_fetch_history()
        self.assertEqual(self.attempts, 2, "one failure, one success, then nothing new to write")

    def test_the_save_says_whether_it_wrote(self):
        self.assertIs(db.save_fetch_history({"r1": {"bot": "alfabot", "state": "pending"}}), False)
        self.assertIs(db.save_fetch_history({"r1": {"bot": "alfabot", "state": "pending"}}), True)


if __name__ == "__main__":
    unittest.main()
