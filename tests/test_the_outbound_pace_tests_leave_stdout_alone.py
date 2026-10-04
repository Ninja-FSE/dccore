"""The outbound-pace tests left sys.stdout swallowed for the rest of the run (#1146).

tests/test_a_shared_outbound_pace.py runs queue_mgr's worker and announce's
debug drain as real threads, and each thread wrapped itself in
contextlib.redirect_stdout(io.StringIO()). That is not thread-safe: it swaps
the process-wide sys.stdout and puts back whatever it found. Two threads that
entered in one order and left in the same order put back the other thread's
StringIO, and every print after that, anywhere in the process, vanished.

Nothing failed. But scripts/function_coverage.py prints its report after
running the suite in-process, and in preflight that report went into the
StringIO: the coverage pass said FAIL with no reason given. The leak followed
the threads' timing, so it came and went between runs.

The tests now silence stdout once, on the main thread, for the whole test.
This runs them a few times and checks that sys.stdout is the stream it was:
with the old threads, each run had about even odds of leaking.
"""

import io
import sys
import unittest

from tests import support  # noqa: F401  (path setup)
from tests import test_a_shared_outbound_pace as pace  # noqa: E402

RUNS = 6


class TheOutboundPaceTestsPutStdoutBack(unittest.TestCase):

    def run_quietly(self, case_class):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(case_class)
        return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)

    def test_two_worker_threads_leave_stdout_as_they_found_it(self):
        for run in range(RUNS):
            before = sys.stdout
            result = self.run_quietly(pace.TheCombinedOutboundRateIsCapped)
            self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
            self.assertIs(sys.stdout, before, f"run {run + 1} left sys.stdout replaced")

    def test_one_worker_thread_does_too(self):
        before = sys.stdout
        result = self.run_quietly(pace.TheStandardLaneIsNoLongerStarvedByVip)

        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertIs(sys.stdout, before)


if __name__ == "__main__":
    unittest.main()
