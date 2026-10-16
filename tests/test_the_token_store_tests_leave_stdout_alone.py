"""The token-store race test left sys.stdout swallowed for the rest of the run (#1379).

tests/test_pair_and_unpair_say_what_was_stored.py runs `pair` and `unpair` on
two console sessions at once, each on its own thread, and each command was
wrapped in contextlib.redirect_stdout(). That swaps the process-wide
sys.stdout and puts back whatever it found. Session B entered first, A entered
second and saved the test's StringIO as "the old stdout"; runtime.admin_tokens_lock
makes A wait for B, so B always left first and A always put the StringIO back.
Unlike #1146 this was not a matter of timing: it leaked on every run.

Nothing failed. But scripts/function_coverage.py prints its report after
running the suite in-process, and the report went into that StringIO: the
coverage pass in preflight said FAIL and gave no reason.

The class now silences stdout once, on the main thread, for the whole test.
This runs it once and checks that sys.stdout and sys.stderr are the streams
they were. Once is enough: the lock fixed the order, so the old code leaked on
every run, where the #1146 guard repeats because its leak followed timing.
"""

import io
import sys
import unittest

from tests import support  # path setup, and the harness's settings below
from tests import test_pair_and_unpair_say_what_was_stored as tokens  # noqa: E402

class TheTokenStoreRaceTestPutsStdoutBack(unittest.TestCase):

    # The class run below is a DCCoreTestCase, so the settings its harness
    # sets are set inside this test too - on purpose, and not this test's
    # leak. Run first in a process, they move from the shipped values.
    SETTINGS_THE_HARNESS_SETS = support.DCCoreTestCase.SETTINGS_THE_HARNESS_SETS

    def setUp(self):
        # If the class under test does leak, the rest of this process must
        # still be able to print - the failure message included.
        self.addCleanup(setattr, sys, "stderr", sys.stderr)
        self.addCleanup(setattr, sys, "stdout", sys.stdout)

    def run_quietly(self, case_class):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(case_class)
        return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)

    def test_two_console_sessions_leave_the_streams_as_they_found_them(self):
        stdout, stderr = sys.stdout, sys.stderr
        result = self.run_quietly(tokens.PairAndUnpairDoNotOverlap)
        leaked_stdout = sys.stdout is not stdout
        leaked_stderr = sys.stderr is not stderr
        sys.stdout, sys.stderr = stdout, stderr

        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual(result.testsRun, 2)
        self.assertFalse(leaked_stdout, "the run left sys.stdout replaced")
        self.assertFalse(leaked_stderr, "the run left sys.stderr replaced")


if __name__ == "__main__":
    unittest.main()
