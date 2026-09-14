"""A rebuild that is still working must not be killed for taking a long time.

Reported from the live bot: `Failed: timed out after 1800s`, on an 80 TB
library that takes hours to walk. The old guard was
`subprocess.run(timeout=LIST_UPDATE_TIMEOUT)` with a flat 1800s default - a
bet that no library takes longer than half an hour.

That bet cannot be fixed by choosing a bigger number, because a wall clock
cannot tell a rebuild that is working from one that is wedged. Any value an
operator picks is either too small for their library or too large to be a
safety net, and the one they have to pick changes every time their library
grows.

The child already reports what it is doing to LIST_PROGRESS_FILE about twice a
second while scanning. So the question worth asking is not "how long has this
taken" but "when did it last do anything", which is what
`run_watching_for_a_stall()` measures.
"""

import io
import json
import os
import subprocess
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class FakeChild:
    """A Popen stand-in that finishes after `ticks` waits.

    Nothing here starts a real process: the rules being tested are about how
    long the watcher is willing to wait and what it looks at while waiting,
    neither of which needs one - and a real child would make these tests take
    as long as the behaviour they describe.
    """

    def __init__(self, ticks=1, returncode=0, stdout="ok", stderr=""):
        # ticks=None means NEVER finishes, which is the only honest fixture
        # for a test whose subject is the watcher giving up. See the note in
        # communicate().
        self.ticks = ticks
        self.returncode = returncode
        self._out = (stdout, stderr)
        self.killed = False
        self.waits = 0

    def communicate(self, timeout=None):
        """Raise TimeoutExpired until `ticks` is used up, then finish.

        A FINITE tick count races the ceiling, and that race is what made
        TheCeilingIsOptional fail on another machine while passing 45 times in
        a row on this one. The watcher's loop does no real waiting against
        this fake, so with ticks=99 and a 1ms ceiling the question is whether
        99 no-op iterations take longer than a millisecond - which depends on
        how warm the filesystem cache is when last_progress_at() reads the
        progress file. Lose that race and the fake FINISHES, the watcher
        returns normally, and the test fails asking why no timeout was raised.

        ticks=None never finishes, so for a test about the watcher giving up
        the only way out of the loop is the thing being tested. No margin, no
        machine dependency.
        """
        self.waits += 1
        if self.killed:
            return self._out
        if self.ticks is not None and self.waits > self.ticks:
            return self._out
        raise subprocess.TimeoutExpired(["child"], timeout)

    def kill(self):
        self.killed = True


class WatchingRatherThanTiming(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.progress = os.path.join(self.make_tree().root, "progress.json")
        self.set_config(LIST_PROGRESS_FILE=self.progress)
        self.child = None
        real_popen = subprocess.Popen
        self.addCleanup(setattr, subprocess, "Popen", real_popen)

    def install(self, child):
        self.child = child
        subprocess.Popen = lambda *a, **k: child
        return child

    def reported(self, seconds_ago):
        with io.open(self.progress, "w", encoding="utf-8") as handle:
            json.dump({"phase": "scanning", "at": time.time() - seconds_ago},
                      handle)

    def test_a_child_that_finishes_is_returned_unchanged(self):
        """The ordinary path, and the contract the caller depends on: the same
        CompletedProcess shape subprocess.run() gave it."""
        self.install(FakeChild(ticks=0, returncode=0, stdout="List of 9 Files"))

        result = commands.run_watching_for_a_stall(["x"], tick=0.01)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "List of 9 Files")

    def test_a_failing_child_is_returned_not_raised(self):
        """A non-zero exit is the caller's to report, with the child's own
        stderr in it - not something to turn into an exception here."""
        self.install(FakeChild(ticks=0, returncode=1, stderr="disk full"))

        result = commands.run_watching_for_a_stall(["x"], tick=0.01)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "disk full")

    def test_a_long_run_that_keeps_reporting_is_left_alone(self):
        """The whole point. Eight hours of honest work must not be killed."""
        child = self.install(FakeChild(ticks=3))
        self.reported(seconds_ago=0)

        commands.run_watching_for_a_stall(["x"], ceiling=0, stall=900,
                                          tick=0.01)

        self.assertFalse(child.killed,
                         "a rebuild that was still reporting progress was "
                         "killed anyway")

    def test_a_child_that_has_gone_quiet_is_killed(self):
        child = self.install(FakeChild(ticks=None))
        self.reported(seconds_ago=1200)

        with self.assertRaises(commands.ListUpdateStalled):
            commands.run_watching_for_a_stall(["x"], stall=900, tick=0.01)

        self.assertTrue(child.killed)

    def test_the_silence_is_measured_and_reported(self):
        """"It stalled" is not actionable; "it reported nothing for twenty
        minutes" is."""
        self.install(FakeChild(ticks=None))
        self.reported(seconds_ago=1200)

        with self.assertRaises(commands.ListUpdateStalled) as caught:
            commands.run_watching_for_a_stall(["x"], stall=900, tick=0.01)

        self.assertGreaterEqual(caught.exception.silent_for, 1200)

    def test_silence_shorter_than_the_window_is_not_a_stall(self):
        """The boundary. Fourteen minutes of quiet during the packing phase is
        a rebuild working, not a rebuild stuck."""
        child = self.install(FakeChild(ticks=2))
        self.reported(seconds_ago=800)

        commands.run_watching_for_a_stall(["x"], stall=900, tick=0.01)

        self.assertFalse(child.killed)


class WhenItCannotTell(DCCoreTestCase):
    """A rebuild that cannot report is not evidence of a rebuild that is
    stuck. Killing one for it turns a cosmetic failure - a full disk, a
    read-only data/ - into the loss of an eight-hour run."""

    def setUp(self):
        super().setUp()
        self.progress = os.path.join(self.make_tree().root, "progress.json")
        self.set_config(LIST_PROGRESS_FILE=self.progress)
        real_popen = subprocess.Popen
        self.addCleanup(setattr, subprocess, "Popen", real_popen)

    def install(self, child):
        subprocess.Popen = lambda *a, **k: child
        return child

    def test_no_progress_file_at_all_is_never_a_stall(self):
        child = self.install(FakeChild(ticks=2))

        commands.run_watching_for_a_stall(["x"], stall=0.001, tick=0.01)

        self.assertFalse(child.killed,
                         "a rebuild that had not written a progress file was "
                         "killed for not having written one")

    def test_an_unreadable_progress_file_is_never_a_stall(self):
        with io.open(self.progress, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        child = self.install(FakeChild(ticks=2))

        commands.run_watching_for_a_stall(["x"], stall=0.001, tick=0.01)

        self.assertFalse(child.killed)

    def test_a_progress_file_with_no_timestamp_is_never_a_stall(self):
        with io.open(self.progress, "w", encoding="utf-8") as handle:
            json.dump({"phase": "scanning"}, handle)
        child = self.install(FakeChild(ticks=2))

        commands.run_watching_for_a_stall(["x"], stall=0.001, tick=0.01)

        self.assertFalse(child.killed)

    def test_last_progress_at_says_none_rather_than_a_number(self):
        """The distinction the rule above rests on. "Cannot tell" must not be
        expressible as a very old timestamp, or every caller has to remember
        to check for it."""
        self.set_config(LIST_PROGRESS_FILE=os.path.join(
            os.path.dirname(self.progress), "nothing-here.json"))

        self.assertIsNone(commands.last_progress_at())


class TheCeilingIsOptional(DCCoreTestCase):
    """0 is the default and means none - but an operator who wants a rebuild
    abandoned after two hours whatever it is doing can still say so."""

    def setUp(self):
        super().setUp()
        self.progress = os.path.join(self.make_tree().root, "progress.json")
        self.set_config(LIST_PROGRESS_FILE=self.progress)
        with io.open(self.progress, "w", encoding="utf-8") as handle:
            json.dump({"phase": "scanning", "at": time.time()}, handle)
        real_popen = subprocess.Popen
        self.addCleanup(setattr, subprocess, "Popen", real_popen)

    def install(self, child):
        subprocess.Popen = lambda *a, **k: child
        return child

    def test_zero_means_no_ceiling(self):
        """Even against a child that never finishes and is reporting happily,
        which is exactly the 80 TB case."""
        child = self.install(FakeChild(ticks=3))

        commands.run_watching_for_a_stall(["x"], ceiling=0, stall=900,
                                          tick=0.01)

        self.assertFalse(child.killed)

    def test_a_ceiling_that_is_set_is_enforced(self):
        child = self.install(FakeChild(ticks=None))

        with self.assertRaises(subprocess.TimeoutExpired):
            commands.run_watching_for_a_stall(["x"], ceiling=0.001, stall=900,
                                              tick=0.01)

        self.assertTrue(child.killed)

    def test_the_ceiling_raises_a_timeout_not_a_stall(self):
        """Two endings, two causes: running past a limit the operator chose is
        not the same event as the library going quiet, and the operator is
        told different things."""
        self.install(FakeChild(ticks=None))

        with self.assertRaises(subprocess.TimeoutExpired):
            commands.run_watching_for_a_stall(["x"], ceiling=0.001, tick=0.01)


class TheDefaultsSayWhatTheyMean(unittest.TestCase):

    def test_there_is_no_hard_cap_by_default(self):
        """The reported bug. 1800s is unindexable for a large library, and no
        default can be right for every library - so the default is to let the
        stall check do the work."""
        self.assertEqual(config.LIST_UPDATE_TIMEOUT, 0)

    def test_the_stall_window_is_generous(self):
        """It has to cover the longest silent step - writing and packing a
        several-hundred-megabyte list on a machine that has just walked 80 TB.
        Killing a rebuild in the last minute of an eight-hour run would be the
        worst possible outcome of a safety net."""
        self.assertGreaterEqual(config.LIST_UPDATE_STALL_SECONDS, 600)

    def test_it_is_still_quicker_than_the_limit_it_replaces(self):
        """A genuinely wedged mount is now noticed sooner than the old flat
        1800s, not later - the change is strictly better at both ends."""
        self.assertLess(config.LIST_UPDATE_STALL_SECONDS, 1800)

    def test_the_packing_phase_still_reports(self):
        """The longest step with nothing else to say. Without this heartbeat
        the stall watch has only the last scanning write to go on while a
        large list is deflated."""
        with io.open(os.path.join(REPO_ROOT, "update_list.py"),
                     encoding="utf-8") as handle:
            body = handle.read().split("def _write_zip_artifact(", 1)[1]

        self.assertIn('write_progress("packing"', body.split("zipfile.ZipFile", 1)[0])


if __name__ == "__main__":
    unittest.main()
