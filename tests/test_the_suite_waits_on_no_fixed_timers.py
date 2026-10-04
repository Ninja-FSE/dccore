"""Two fixed waits cost the suite about half a minute on every run (#1148).

The freeze countdown (dcc.freeze_absent_user's user_queue_timer) slept a
literal ten seconds per step. One dispatch test queues a user who is not in
the channel, which starts that countdown, and then had nothing to do but wait
out its settle() deadline twice: twenty seconds for one test. The step is now
the module constant dcc.FREEZE_POLL_SECONDS, with the same ten-second value,
and the countdown reads it on EVERY pass rather than binding it once, so a
test can shorten it and a !rehash that changes it reaches a countdown that is
already running.

The deep-record forgetting tests filled their record with 630 separate
record_sent() calls, each reopening the file, re-running its pragmas and
schema and checkpointing the log: 18 ms a row. The fixture now builds the
same rows with record_sent() and writes them in one transaction. The second
class below holds it to that: the fast fixture must leave the file with the
same rows the slow one did.
"""

import io
import sys
import threading
import time
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import transfer_log  # noqa: E402

from tests import test_a_record_of_finished_transfers as record_tests  # noqa: E402
from tests.support import DCCoreTestCase, RecordingSocket, no_disk_writes, queue_row, silence_debug  # noqa: E402

HERE = "#dccore-test"   # queue_row()'s channel


class TheFreezeCountdownReadsItsStepOnEveryPass(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        no_disk_writes(db)
        self.addCleanup(setattr, sys, "stdout", sys.stdout)
        sys.stdout = io.StringIO()
        self.addCleanup(setattr, dcc, "FREEZE_POLL_SECONDS", dcc.FREEZE_POLL_SECONDS)
        self.addCleanup(setattr, dcc, "FREEZE_TIMEOUT", dcc.FREEZE_TIMEOUT)
        config.bot_joined_channel = True
        config.channel_users = {HERE: {"someoneelse"}}      # synced, and dave is not in it
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Song.flac")]

        # Record the step the countdown thread asks for, and sleep only a
        # moment whatever it asks: a step bound once to ten seconds must show
        # up here as a recorded 10, not as a ten-second test. Every other
        # thread sleeps for real. Each step first waits for self.go, so a
        # test can hold the countdown before its first pass.
        self.steps = []
        self.countdown = None
        self.go = threading.Event()
        real_sleep = time.sleep

        def recording_sleep(seconds):
            if threading.current_thread() is self.countdown:
                self.go.wait(10)
                self.steps.append(seconds)
                real_sleep(0.001)
            else:
                real_sleep(seconds)
        patcher = mock.patch.object(time, "sleep", recording_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.go.set()
        with dcc.queue_lock:
            config.frozen_queues.pop("dave", None)
        if self.countdown is not None:
            self.countdown.join(10)
            self.assertFalse(self.countdown.is_alive(), "the countdown thread is still running")
        super().tearDown()

    def start_countdown(self):
        started = []
        real_thread = threading.Thread

        class Catch(real_thread):
            def start(inner):
                started.append(inner)
                self.countdown = inner
                real_thread.start(inner)
        with mock.patch.object(threading, "Thread", Catch):
            dcc.freeze_absent_user(RecordingSocket(), "dave", HERE)
        self.assertEqual(len(started), 1, "the freeze did not start one countdown")

    def wait_for(self, condition, what):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not condition():
            time.sleep(0.005)
        self.assertTrue(condition(), what)

    def test_the_step_is_the_module_constant_and_a_change_reaches_a_running_countdown(self):
        dcc.FREEZE_POLL_SECONDS = 0.01
        self.go.set()
        self.start_countdown()
        self.wait_for(lambda: len(self.steps) >= 2, "the countdown never stepped")
        self.assertEqual(set(self.steps), {0.01}, "the countdown does not step by FREEZE_POLL_SECONDS")

        dcc.FREEZE_POLL_SECONDS = 0.02
        self.wait_for(lambda: 0.02 in self.steps,
                      "a changed FREEZE_POLL_SECONDS never reached the running countdown: %r"
                      % sorted(set(self.steps)))

        with dcc.queue_lock:
            config.frozen_queues.pop("dave", None)
        self.countdown.join(10)
        self.assertFalse(self.countdown.is_alive(), "a thawed user's countdown did not end")
        self.assertIn("dave", config.dcc_queue, "a thawed user's queue must be kept")

    def test_the_constant_keeps_the_ten_second_step(self):
        self.assertEqual(dcc.FREEZE_POLL_SECONDS, 10.0)

    def test_an_unreadable_timestamp_counts_one_step_per_pass(self):
        """The fallback adds the step it slept, not a literal ten seconds."""
        dcc.FREEZE_POLL_SECONDS = 0.01
        dcc.FREEZE_TIMEOUT = 0.05
        self.start_countdown()
        # Held at its first step: every pass from here on reads the bad value.
        with dcc.queue_lock:
            config.frozen_queues["dave"] = "not a time"
        self.go.set()
        self.countdown.join(10)

        self.assertFalse(self.countdown.is_alive())
        self.assertNotIn("dave", config.dcc_queue, "the countdown ran out and erased the queue")
        self.assertGreaterEqual(len(self.steps), 5,
                                "the fallback counted more than the step it slept: %r" % (self.steps,))


class TheDeepRecordFixtureWritesWhatRecordSentWrites(record_tests.Case):
    """fill_many() must leave the same rows that 630 record_sent() calls do.

    Run at a small size, so the slow way stays cheap; the comparison leaves
    out the id and the second each row was stamped with.
    """

    def content(self):
        return [row[1:4] + row[5:] for row in self.rows()]

    def test_the_rows_match_one_record_sent_per_row(self):
        record_tests.ForgettingReallyRemoves.fill_many(self, victims=3, others=12)
        fast = self.content()
        transfer_log.forget_all()
        self.assertEqual(self.rows(), [])

        for n in range(12):
            self.sent(f"Some Album/Track {n}.flac", nick=f"nick{n % 40}")
            if n % 4 == 0:
                self.sent(f"Private/Secret {n}.flac", nick="SecretVictim")
        slow = self.content()

        self.assertEqual(len(fast), 15)
        self.assertEqual(fast, slow)


if __name__ == "__main__":
    unittest.main()
