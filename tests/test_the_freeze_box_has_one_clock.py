"""The freeze box had two clocks that disagreed (audit M50, #652).

A user who leaves keeps their queue for five minutes. Two things measured
those minutes. The per-user timer thread counted ten seconds at a time and
refused to count while the bot itself was offline - "the bot's own downtime
must NEVER count against a user's queue". The sweep at the top of
check_queue_and_send() compared the frozen timestamp with wall time, and
ran the moment bot_joined_channel came back - which activation sets as soon
as ANY channel's NAMES has populated channel_users, twenty seconds after
the join if the rest never answer. So: X leaves #b, the bot loses its link
a minute later and is back eleven minutes after that, #a's NAMES arrives,
the wake-up sweep runs, and X's thirty files and temp archives are deleted
with "frozen for over five minutes and never came back" - while the timer
thread still says sixty seconds elapsed, and #b's NAMES has not even
arrived.

One clock now. The disconnect epilogue stops it (pause_freeze_clock) and
activation restarts it (resume_freeze_clock) by moving every frozen
timestamp forward by the outage, so the sweep, the timer thread - which
now reads the same timestamp instead of counting on its own - and the
console's seconds-left all measure the same thing: time the bot has been
ONLINE since the freeze. And neither the sweep nor the timer deletes a
queue whose channel the bot has no member list for yet: until that
channel's NAMES arrives, absence is not an observation.
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

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket, no_disk_writes, queue_row, silence_debug  # noqa: E402
from tests.test_reconnect import no_threads, quiet  # noqa: E402

OUTAGE = 660.0          # eleven minutes away
HERE = "#dccore-test"   # queue_row()'s channel
ELSEWHERE = "#otherchannel"


class TheClockStopsWithTheLink(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, runtime, "freeze_clock_paused_at", None)
        runtime.freeze_clock_paused_at = None

    def test_the_outage_is_added_to_every_frozen_timestamp(self):
        now = time.time()
        config.frozen_queues.update({"dave": now - 60.0, "erin": now - 200.0})
        dcc.pause_freeze_clock(now=now)

        skipped = dcc.resume_freeze_clock(now=now + OUTAGE, log=lambda _m: None)

        self.assertEqual(skipped, OUTAGE)
        self.assertAlmostEqual(config.frozen_queues["dave"], now - 60.0 + OUTAGE)
        self.assertAlmostEqual(config.frozen_queues["erin"], now - 200.0 + OUTAGE)
        self.assertIsNone(runtime.freeze_clock_paused_at)

    def test_a_second_pause_keeps_the_first_moment(self):
        dcc.pause_freeze_clock(now=1000.0)
        dcc.pause_freeze_clock(now=1500.0)

        self.assertEqual(runtime.freeze_clock_paused_at, 1000.0)

    def test_a_resume_without_a_pause_changes_nothing(self):
        config.frozen_queues["dave"] = 123.0

        self.assertEqual(dcc.resume_freeze_clock(now=999.0, log=lambda _m: None), 0.0)
        self.assertEqual(config.frozen_queues["dave"], 123.0)

    def test_it_says_what_it_did(self):
        config.frozen_queues["dave"] = 1.0
        dcc.pause_freeze_clock(now=1000.0)
        logs = []

        dcc.resume_freeze_clock(now=1030.0, log=logs.append)

        self.assertIn("away 30s", logs[0])
        self.assertIn("1 frozen queue", logs[0])


class TheAuditsScenario(DCCoreTestCase):
    """X left at T; the bot was away from T+1 min to T+12 min; the sweep runs
    as soon as the bot is back. With one clock X has one minute on the
    countdown, not twelve."""

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        no_disk_writes(db)
        self.addCleanup(setattr, runtime, "freeze_clock_paused_at", None)
        runtime.freeze_clock_paused_at = None
        self.sock = RecordingSocket()
        self.rows = [queue_row(user="dave", filename="Song.flac")]
        config.dcc_queue["dave"] = self.rows
        config.bot_joined_channel = True
        config.channel_users = {HERE: {"someoneelse"}}      # #b synced, dave not in it
        now = time.time()
        config.frozen_queues["dave"] = now - 60.0 - OUTAGE   # frozen 12 min ago by the wall
        self.went_down_at = now - OUTAGE

    def sweep(self):
        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "system_next_trigger_fallback")

    def test_without_the_rebase_the_sweep_would_delete_it(self):
        """The defect, kept as the control: twelve wall-clock minutes."""
        self.sweep()

        self.assertNotIn("dave", config.dcc_queue)

    def test_with_the_outage_taken_out_the_queue_survives(self):
        dcc.pause_freeze_clock(now=self.went_down_at)
        dcc.resume_freeze_clock(log=lambda _m: None)   # now: eleven minutes later

        self.sweep()

        self.assertIn("dave", config.dcc_queue, "twelve minutes by the wall, one online")
        self.assertIn("dave", config.frozen_queues)

    def test_and_the_consoles_seconds_left_agree(self):
        """adminchat's QUEUE row derives seconds-left from the same timestamp."""
        import adminchat
        dcc.pause_freeze_clock(now=self.went_down_at)
        dcc.resume_freeze_clock(log=lambda _m: None)

        left = adminchat.FREEZE_TIMEOUT - (time.time() - config.frozen_queues["dave"])

        self.assertGreater(left, 200)


class AChannelWithNoMemberListYetIsNotEvidence(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        no_disk_writes(db)
        self.sock = RecordingSocket()
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Song.flac", channel=ELSEWHERE)]
        config.frozen_queues["dave"] = time.time() - 400.0
        config.bot_joined_channel = True
        config.channel_users = {HERE: {"someoneelse"}}     # ELSEWHERE's NAMES not in yet

    def sweep(self):
        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "system_next_trigger_fallback")

    def test_the_sweep_waits_for_that_channels_names(self):
        self.assertFalse(dcc.frozen_users_channel_is_synced("dave"))

        self.sweep()

        self.assertIn("dave", config.dcc_queue)
        self.assertIn("dave", config.frozen_queues)

    def test_and_deletes_once_it_has_them_and_the_user_is_not_there(self):
        config.channel_users[ELSEWHERE] = {"someoneelse"}
        self.assertTrue(dcc.frozen_users_channel_is_synced("dave"))

        self.sweep()

        self.assertNotIn("dave", config.dcc_queue)

    def test_a_queue_without_a_channel_is_judged_as_before(self):
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Song.flac", channel="")]

        self.assertTrue(dcc.frozen_users_channel_is_synced("dave"))


class TheTimerThreadReadsTheSameClock(DCCoreTestCase):
    """user_queue_timer used to count ten seconds per loop on its own. It now
    reads the elapsed time off the frozen timestamp: with the timestamp
    already past the timeout and the sleep stubbed, ONE pass ends it - the
    old counter needed thirty."""

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        no_disk_writes(db)
        self.sleeps = []
        self.go = threading.Event()   # the countdown's sleep parks here until the test says so
        real_sleep = dcc.time.sleep

        def parked_sleep(seconds):
            self.sleeps.append(seconds)
            self.go.wait(10)
        dcc.time.sleep = parked_sleep
        self.addCleanup(setattr, dcc.time, "sleep", real_sleep)
        config.bot_joined_channel = True
        config.channel_users = {HERE: {"someoneelse"}}
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Song.flac")]
        self.thread = None

    def tearDown(self):
        self.go.set()
        if self.thread is not None:
            self.thread.join(10)
            self.assertFalse(self.thread.is_alive(), "the countdown thread is still running")
        super().tearDown()

    def start_countdown(self):
        """freeze_absent_user() starts the timer on a thread of its own; catch it."""
        started = []
        real_thread = dcc.threading.Thread

        class Catch(real_thread):
            def start(inner):
                started.append(inner)
                real_thread.start(inner)
        dcc.threading.Thread = Catch
        try:
            with quiet():
                dcc.freeze_absent_user(RecordingSocket(), "dave", HERE)
        finally:
            dcc.threading.Thread = real_thread
        self.assertEqual(len(started), 1)
        self.thread = started[0]

    def test_a_timestamp_past_the_timeout_ends_the_countdown_in_one_pass(self):
        self.start_countdown()
        # The thread is parked in its first sleep. Move the timestamp - as
        # resume_freeze_clock() moves it the other way - then let it go.
        with dcc.queue_lock:
            config.frozen_queues["dave"] = time.time() - (dcc.FREEZE_TIMEOUT + 5)
        self.go.set()

        self.thread.join(10)

        self.assertFalse(self.thread.is_alive())
        self.assertNotIn("dave", config.dcc_queue, "the countdown ended and erased the queue")
        self.assertEqual(len(self.sleeps), 1, "it counted on its own instead of reading the clock")


class TheReadLoopStartsAndStopsTheClock(unittest.TestCase):

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_disconnect_epilogue_pauses_it(self):
        epilogue = self.source().split("Lost the connection. Reconnecting", 1)[1][:600]

        self.assertIn("dcc.pause_freeze_clock()", epilogue)

    def test_activation_resumes_it_before_the_wake_up_sweep(self):
        activate = self.source().split("config.bot_joined_channel = True", 1)[1][:1200]

        self.assertIn("dcc.resume_freeze_clock()", activate)
        self.assertLess(activate.index("dcc.resume_freeze_clock()"),
                        activate.index("dcc.wake_restored_queues"))


if __name__ == "__main__":
    unittest.main()
