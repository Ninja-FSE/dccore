"""A late write from a thread that outlived its test must never reach the
operator's own data/ files.

dcc.py's start_dcc_send() dispatches on threads the test that started them
does not join before returning - test_dcc_resume_end_to_end.py and
test_queue_progress_is_recorded.py both start a real one directly, and #430's
own dispatch tests do the same. Every exit path settles the queue row through
db.save_dcc_queue(); a SUCCESSFUL one additionally settles the transfer
itself through db.update_stats_on_complete() and db.record_download(). All
three read module-level path constants that db.py derives from config at
IMPORT, so a `!rehash` test's reload of db re-derives all three from
whatever config says at that moment too.

DCCoreTestCase.tearDown() used to restore all three to the REAL path once a
test finished - which is exactly the window a late tick from one of those
outliving threads can land in. It was caught doing so for the queue file: a
two-byte write, i.e. an EMPTY queue, meaning running the suite on a live
install silently dropped every transfer anybody had queued. The other two
were found by inspection of the identical call path rather than by being
caught outright.

The fix parks all three - both the db.X name the writer reads and the
config.X name a db reload re-derives it from - on a dead sink path instead,
as the LAST cleanup to run (registered FIRST in setUp(), and unittest runs
cleanups LIFO). This drives the real setUp()/test/tearDown() cycle rather
than asserting against the source, since what actually matters is the
CLEANUP ORDER - a source read cannot tell "registered first" apart from
"registered last".
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class _ProbeCase(DCCoreTestCase):
    """A single do-nothing test, run programmatically below so its full
    setUp()/tearDown() cycle happens exactly the way the real suite's does."""

    def test_nothing(self):
        pass


def _run_one_probe():
    """Runs _ProbeCase.test_nothing to completion and returns nothing - the
    caller inspects module state afterward, which is the whole point: this
    is what every OTHER test in the suite leaves behind once it finishes."""
    suite = unittest.TestSuite([_ProbeCase("test_nothing")])
    with open(os.devnull, "w") as sink:
        unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)


class NoRealDataFileIsEverLeftAsTheActiveTarget(unittest.TestCase):

    def setUp(self):
        self._real_config_dcc_queue = getattr(
            sys.modules["defaults"], "DCC_QUEUE_FILE", None)
        self._real_config_speed_record = getattr(
            sys.modules["defaults"], "SPEED_RECORD_FILE", None)
        self._real_config_download_counts = getattr(
            sys.modules["defaults"], "DOWNLOAD_COUNTS_FILE", None)

    def tearDown(self):
        # Restore whatever the module-level defaults were before this test's
        # own probe ran, so a run of this file does not itself leave the
        # sink paths behind for tests that come after it.
        for name, value in (
            ("DCC_QUEUE_FILE", self._real_config_dcc_queue),
            ("SPEED_RECORD_FILE", self._real_config_speed_record),
            ("DOWNLOAD_COUNTS_FILE", self._real_config_download_counts),
        ):
            if value is None:
                continue
            setattr(sys.modules["defaults"], name, value)
            setattr(db, name, value)

    def test_the_queue_file_is_parked_on_a_dead_path_after_the_test_ends(self):
        _run_one_probe()

        self.assertNotIn("data" + os.sep, db.DCC_QUEUE_FILE)
        self.assertIn("dccore-orphaned-test-write", db.DCC_QUEUE_FILE)

    def test_the_speed_record_file_is_parked_too(self):
        _run_one_probe()

        self.assertNotIn("data" + os.sep, db.SPEED_RECORD_FILE)
        self.assertIn("dccore-orphaned-test-write", db.SPEED_RECORD_FILE)

    def test_the_download_counts_file_is_parked_too(self):
        _run_one_probe()

        self.assertNotIn("data" + os.sep, db.DOWNLOAD_COUNTS_FILE)
        self.assertIn("dccore-orphaned-test-write", db.DOWNLOAD_COUNTS_FILE)

    def test_config_carries_the_same_dead_path_not_just_db(self):
        """The reload half of the bug: a !rehash test's importlib.reload(db)
        re-derives every one of these from config, not from whatever db
        already held. Parking db.X alone would survive right up until the
        next reload silently undid it."""
        _run_one_probe()

        config = sys.modules["defaults"]
        for name in ("DCC_QUEUE_FILE", "SPEED_RECORD_FILE", "DOWNLOAD_COUNTS_FILE"):
            with self.subTest(name=name):
                self.assertEqual(getattr(config, name), getattr(db, name),
                                 f"config.{name} and db.{name} disagree - a "
                                 f"reload would re-derive db.{name} from "
                                 f"config's (real) value")

    def test_the_three_sinks_are_three_different_files(self):
        """Not one shared path for all three - a genuine write from one
        writer landing in another's sink would still be silently lost, just
        differently than intended."""
        _run_one_probe()

        paths = {db.DCC_QUEUE_FILE, db.SPEED_RECORD_FILE, db.DOWNLOAD_COUNTS_FILE}
        self.assertEqual(len(paths), 3)


if __name__ == "__main__":
    unittest.main()
