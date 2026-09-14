"""Queue survival across an IRC disconnect.

Every case here guards a defect that actually shipped:

  * dcc.check_queue_and_send used to freeze - and five minutes later delete - the
    personal queue of any user who was not found in config.channel_users, without
    first asking whether the BOT was synced. During a reconnect channel_users is
    empty or half-populated, so a netsplit looked exactly like every user having
    left, and their queues were erased.
  * the stale-freeze sweep at the top of the same function ran unconditionally and
    would delete queues while the bot was still off the net.
  * the sweep had no "thaw" step: a user who was demonstrably back in the channel
    still lost their queue once the freeze timestamp passed 300s.
  * queue_mgr.queue_worker popped messages off both lanes BEFORE checking whether a
    socket existed, so everything queued during a reconnect was silently dropped.
  * the NAMES-sync thaw sent one debug line per channel, all landing within the
    same few seconds after a reconnect - the exact burst that let a 14-channel
    reconnect flood a bot off the network (#406).
"""

import contextlib
import io
import os
import threading
import time
import unittest

from tests.support import (DCCoreTestCase, queue_row, silence_debug,
                           no_disk_writes, CapturedDispatch, RecordingSocket,
                           DeadSocket)

import defaults as config
import announce
import db
import dcc
import irc
import queue_mgr
import runtime


FREEZE_TIMEOUT = 300.0  # dcc.check_queue_and_send's hard-coded stale-freeze timeout


@contextlib.contextmanager
def quiet():
    """Swallow the daemon's very chatty print() output for the duration of a call."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


class _FakeThread:
    """A threading.Thread stand-in that records the target and never runs it.

    check_queue_and_send answers a freeze by spawning user_queue_timer, which then
    sleeps in ten second steps for five minutes. Letting that thread exist would
    leak a mutator of config into the rest of the suite, so the freeze tests assert
    on the state the call leaves behind instead.
    """

    spawned = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **extra):
        self.target = target
        self.args = args
        self.daemon = daemon
        _FakeThread.spawned.append((getattr(target, "__name__", str(target)), args))

    def start(self):
        return None

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


@contextlib.contextmanager
def no_threads():
    """Neutralise every thread dcc spawns during the block."""
    real = dcc.threading.Thread
    _FakeThread.spawned = []
    dcc.threading.Thread = _FakeThread
    try:
        yield _FakeThread.spawned
    finally:
        dcc.threading.Thread = real


class ReconnectQueueSurvivalTests(DCCoreTestCase):
    """dcc.check_queue_and_send must not mistake the bot's own downtime for a part."""

    def setUp(self):
        super().setUp()
        self._real_send_debug = announce.send_debug
        self._real_save_queue = db.save_dcc_queue
        self._real_save_bans = db.save_bans_to_file
        self._real_save_stats = db.save_advanced_stats
        self.debug = silence_debug(announce)
        no_disk_writes(db)
        self.sock = RecordingSocket()
        # Two rows for "dave", the identical shape dcc.py builds at request time.
        self.rows = [queue_row(user="dave", filename="Song.flac"),
                     queue_row(user="dave", filename="Other.flac")]
        config.dcc_queue["dave"] = self.rows

    def tearDown(self):
        announce.send_debug = self._real_send_debug
        db.save_dcc_queue = self._real_save_queue
        db.save_bans_to_file = self._real_save_bans
        db.save_advanced_stats = self._real_save_stats
        super().tearDown()

    def assert_queue_untouched(self):
        self.assertIn("dave", config.dcc_queue)
        self.assertIs(config.dcc_queue["dave"], self.rows)
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]],
                         ["Song.flac", "Other.flac"])

    # ------------------------------------------------------------------
    # The bot is not channel-synced: hands off the queue entirely.
    # ------------------------------------------------------------------

    def test_no_freeze_while_bot_is_disconnected(self):
        """Defect: a reconnect (bot_joined_channel False) froze and later deleted queues."""
        config.bot_joined_channel = False
        # channel_users can even carry stale members from before the split; the
        # decisive fact is that the bot itself has not finished joining.
        config.channel_users = {"#dccore-test": {"someone_else"}}

        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(config.frozen_queues, {},
                         "an unsynced bot must never start a countdown")
        self.assert_queue_untouched()

    def test_no_freeze_while_channel_users_is_empty(self):
        """Defect: a half-synced reconnect (empty channel_users) froze innocent users."""
        config.bot_joined_channel = True
        config.channel_users = {}

        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(config.frozen_queues, {})
        self.assert_queue_untouched()

    def test_unsynced_call_dispatches_nothing(self):
        """Defect guard: no DCC send may be started while the bot is off the net."""
        config.bot_joined_channel = False
        config.channel_users = {}

        with CapturedDispatch(dcc) as dispatch, quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(dispatch.calls, [])
        self.assertEqual(config.frozen_queues, {})
        self.assert_queue_untouched()

    def test_sweep_does_not_run_while_unsynced(self):
        """Defect: the stale-freeze sweep deleted queues during the bot's own downtime."""
        config.bot_joined_channel = False
        config.channel_users = {}
        stale = time.time() - (FREEZE_TIMEOUT + 600)
        config.frozen_queues["dave"] = stale

        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertIn("dave", config.frozen_queues,
                      "the sweep must be gated on the bot being synced")
        self.assertEqual(config.frozen_queues["dave"], stale)
        self.assert_queue_untouched()

    def test_sweep_does_not_run_while_unsynced_for_a_third_party(self):
        """Defect: the sweep is global, so it erased OTHER users' queues on reconnect."""
        config.bot_joined_channel = False
        config.channel_users = {}
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="Erin.flac")]
        config.frozen_queues["erin"] = time.time() - (FREEZE_TIMEOUT + 600)

        with no_threads(), quiet():
            # A completed transfer for a completely different user drives the sweep.
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertIn("erin", config.dcc_queue)
        self.assertIn("erin", config.frozen_queues)

    # ------------------------------------------------------------------
    # The bot IS synced: the freeze machinery works as designed.
    # ------------------------------------------------------------------

    def test_absent_user_is_frozen_and_queue_retained(self):
        """Guards the real freeze path: synced bot plus a genuinely absent user."""
        config.bot_joined_channel = True
        config.channel_users = {"#dccore-test": {"someone_else", "OpGuy"}}

        with no_threads() as spawned, quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertIn("dave", config.frozen_queues)
        self.assertAlmostEqual(config.frozen_queues["dave"], time.time(), delta=5.0)
        # The queue is kept for the whole countdown - only the timer may erase it.
        self.assert_queue_untouched()
        self.assertIn("user_queue_timer", [name for name, _ in spawned])
        self.assertTrue(any(cat == "QUIT" for cat, _ in self.debug))

    def test_repeated_calls_do_not_restart_the_countdown(self):
        """Defect: every trigger re-stamped frozen_queues, so the timer never expired."""
        config.bot_joined_channel = True
        config.channel_users = {"#dccore-test": {"someone_else"}}

        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "dave")
        first_stamp = config.frozen_queues["dave"]

        # Rewind the stamp so a naive re-freeze would be obvious, then hit the
        # function again the way the 3s fallback trigger does.
        rewound = first_stamp - 120.0
        config.frozen_queues["dave"] = rewound

        with no_threads() as spawned, quiet():
            dcc.check_queue_and_send(self.sock, "dave")
            dcc.check_queue_and_send(self.sock, "DAVE")  # also case-insensitive

        self.assertEqual(config.frozen_queues["dave"], rewound,
                         "an existing countdown must not be reset or stacked")
        self.assertEqual([name for name, _ in spawned].count("user_queue_timer"), 0,
                         "no second countdown thread may be started")
        self.assert_queue_untouched()

    def test_freeze_inside_the_timeout_keeps_the_queue(self):
        """Guards retention: a fresh freeze must survive the sweep untouched."""
        config.bot_joined_channel = True
        config.channel_users = {"#dccore-test": {"someone_else"}}
        recent = time.time() - 100.0
        config.frozen_queues["dave"] = recent

        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(config.frozen_queues["dave"], recent)
        self.assert_queue_untouched()

    def test_sweep_thaws_a_returning_user_instead_of_deleting(self):
        """Defect: a user back in channel_users still lost their queue at timeout."""
        config.bot_joined_channel = True
        # Mixed case on purpose - IRC nicks come back from NAMES in any case.
        config.channel_users = {"#dccore-test": {"Dave", "OpGuy"}}
        config.frozen_queues["dave"] = time.time() - (FREEZE_TIMEOUT + 60)

        with CapturedDispatch(dcc) as dispatch, quiet():
            dcc.check_queue_and_send(self.sock, "Dave")

        self.assertNotIn("dave", config.frozen_queues,
                         "a present user must be thawed, not swept")
        self.assert_queue_untouched()
        # Thawed means live again: the head of the queue goes straight out.
        self.assertEqual(dispatch.files, ["Song.flac"])

    def test_thaw_applies_to_a_third_party_in_the_sweep(self):
        """Defect: the thaw must cover every frozen user the sweep visits, not just the caller."""
        config.bot_joined_channel = True
        config.channel_users = {"#dccore-test": {"erin"}}
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="Erin.flac")]
        config.frozen_queues["erin"] = time.time() - (FREEZE_TIMEOUT + 600)

        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertNotIn("erin", config.frozen_queues)
        self.assertIn("erin", config.dcc_queue)
        self.assertEqual(len(config.dcc_queue["erin"]), 1)

    # ------------------------------------------------------------------
    # Control cases: the designed deletion must still happen.
    # ------------------------------------------------------------------

    def test_stale_freeze_deletes_queue_when_user_is_really_gone(self):
        """Control: synced bot, absent user, freeze older than 300s -> queue erased."""
        config.bot_joined_channel = True
        config.channel_users = {"#dccore-test": {"OpGuy"}}
        config.frozen_queues["dave"] = time.time() - (FREEZE_TIMEOUT + 1)

        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertNotIn("dave", config.dcc_queue,
                         "the designed cleanup must still fire")
        self.assertNotIn("dave", config.frozen_queues)

    def test_stale_freeze_removes_the_temporary_archive_from_disk(self):
        """Control: a swept queue must not leak its temporary .rar on the tmp volume."""
        self.make_tree()
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        archive = os.path.join(config.TMP_ZIP_DIR, "Black_Album.rar")
        with open(archive, "wb") as handle:
            handle.write(b"RAR!")

        config.bot_joined_channel = True
        config.channel_users = {"#dccore-test": {"OpGuy"}}
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Black_Album.rar",
                                              path=archive, is_temporary_zip=True)]
        config.frozen_queues["dave"] = time.time() - (FREEZE_TIMEOUT + 1)

        with no_threads(), quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertNotIn("dave", config.dcc_queue)
        self.assertFalse(os.path.exists(archive))


class _SleepShim:
    """Replacement for queue_mgr's `time` module.

    Caps every sleep so the worker spins fast in the test, and raises SystemExit
    once stopped. SystemExit is not an Exception, so it escapes queue_worker's
    catch-all handler and ends the thread - the worker has no other exit.
    """

    def __init__(self, cap=0.02):
        self.cap = cap
        self.stopped = threading.Event()

    def sleep(self, seconds):
        if self.stopped.is_set():
            raise SystemExit
        time.sleep(min(float(seconds), self.cap))
        if self.stopped.is_set():
            raise SystemExit

    def time(self):
        return time.time()


class QueueWorkerReconnectTests(DCCoreTestCase):
    """queue_mgr.queue_worker must hold, not discard, while the socket is gone."""

    def setUp(self):
        super().setUp()
        # SMALL, not the operator-facing default (5.0) or even 0.05 this used
        # to read: the pacing sleep this pays now lives in
        # runtime.OutboundPacer.wait_for_slot() (#406), which loops against
        # time.monotonic() rather than sleeping a single, cappable duration -
        # so _SleepShim's cap on queue_mgr.time.sleep() below no longer
        # shortens it at all, it only makes the loop check more often over
        # the SAME real wall-clock span. The value itself is what has to be
        # small now.
        config.MSG_DELAY = 0.01
        config.vip_queue = []
        config.send_queue = {}
        self._real_time = queue_mgr.time
        self.shim = _SleepShim()
        queue_mgr.time = self.shim
        self.worker = None
        # A fresh clock per test - runtime.outbound_pacer is a process-wide
        # singleton, and a reservation left over from whichever test ran
        # immediately before this one must not delay this one's first send.
        self._real_pacer = runtime.outbound_pacer
        runtime.outbound_pacer = runtime.OutboundPacer()
        # DEFENSIVE, against a pre-existing gap this shared pacer makes worse:
        # once any earlier test in the suite triggers a real send_debug()
        # without silencing it, announce._ensure_debug_drain() starts a REAL
        # background thread that never stops for the rest of the process -
        # _debug_drain_started has no reset. That thread re-reads
        # sys.modules['oserve'] and runtime.outbound_pacer on every loop, so
        # it happily attaches itself to whatever THIS test's fake oserve and
        # fresh pacer are - writing stray debug lines into this test's own
        # RecordingSocket and eating shared-clock slots meant for it. This
        # cannot stop such a thread (it is not this test's to stop), only
        # keep it from having anything queued to drain at the start.
        announce._debug_queue.clear()

    def tearDown(self):
        self.shim.stopped.set()
        if self.worker is not None:
            self.worker.join(timeout=3.0)
        queue_mgr.time = self._real_time
        runtime.outbound_pacer = self._real_pacer
        super().tearDown()

    def start_worker(self):
        def run():
            # The worker prints on every send; keep the test output readable.
            with contextlib.redirect_stdout(io.StringIO()):
                queue_mgr.queue_worker()

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    # 5.0s, not 1.0s: a stray real debug-drain thread from an earlier,
    # unrelated test (see the setUp comment above) competes for the same
    # shared runtime.outbound_pacer, which can delay - never lose - this
    # worker's own sends past a short window under a full-suite run. A
    # ceiling, not a delay: the ordinary case is still done in well under a
    # second.
    def wait_until(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_worker_holds_messages_while_socket_is_none(self):
        """Defect: both lanes popped before testing the socket, so a reconnect ate them."""
        self.oserve.irc_connection = None
        config.vip_queue.append("PRIVMSG #dccore-test :vip line\r\n")
        config.send_queue["dave"] = ["NOTICE dave :held line\r\n"]

        self.start_worker()
        # Long enough for many loop iterations at the capped sleep length.
        time.sleep(0.4)

        self.assertEqual(config.vip_queue, ["PRIVMSG #dccore-test :vip line\r\n"],
                         "VIP lane must not drain into a void")
        self.assertEqual(config.send_queue.get("dave"), ["NOTICE dave :held line\r\n"],
                         "the standard lane must not drain into a void")

    def test_worker_delivers_held_messages_once_a_socket_returns(self):
        """Defect: messages queued during a reconnect never reached the new socket."""
        self.oserve.irc_connection = None
        config.vip_queue.append("PRIVMSG #dccore-test :vip line\r\n")
        config.send_queue["dave"] = ["NOTICE dave :held line\r\n"]

        self.start_worker()
        time.sleep(0.2)
        self.assertTrue(config.vip_queue, "precondition: the VIP line is still held")

        sock = RecordingSocket()
        self.oserve.irc_connection = sock

        delivered = self.wait_until(
            lambda: "vip line" in sock.text() and "held line" in sock.text())
        self.assertTrue(delivered,
                        "held messages must flush on reconnect, got: " + repr(sock.text()))
        self.assertEqual(config.vip_queue, [])
        # The worker drops the user key only after the post-send MSG_DELAY pause,
        # so "drained" here means "holds nothing", key present or not.
        self.assertTrue(self.wait_until(lambda: not config.send_queue.get("dave")),
                        "the standard lane must be drained after reconnect")

    def test_worker_survives_a_dead_socket_and_flushes_on_the_next_one(self):
        """Defect: a socket.error used to `break` the loop and kill the pump for good."""
        self.oserve.irc_connection = DeadSocket()
        config.vip_queue.append("PRIVMSG #dccore-test :lost vip\r\n")
        config.send_queue["dave"] = ["NOTICE dave :second line\r\n"]

        self.start_worker()
        time.sleep(0.3)

        sock = RecordingSocket()
        self.oserve.irc_connection = sock
        config.vip_queue.append("PRIVMSG #dccore-test :fresh vip\r\n")

        self.assertTrue(self.wait_until(lambda: "fresh vip" in sock.text()),
                        "the worker thread must still be pumping after a broken pipe")
        self.assertTrue(self.worker.is_alive())


class ReconnectThawSummaryTests(DCCoreTestCase):
    """#406: a NAMES sync fires once per channel, all within a few seconds of
    the same JOIN command - one debug line per channel there is the burst
    that flooded a 14-channel bot off the network. Batched into one line per
    burst instead."""

    def setUp(self):
        super().setUp()
        self.captured = silence_debug(announce)
        self._real_quiet_seconds = irc._RECONNECT_THAW_QUIET_SECONDS
        irc._RECONNECT_THAW_QUIET_SECONDS = 0.05

    def tearDown(self):
        timer = irc._reconnect_thaw_summary["timer"]
        if timer is not None:
            timer.cancel()
        irc._reconnect_thaw_summary["total"] = 0
        irc._reconnect_thaw_summary["channels"] = set()
        irc._reconnect_thaw_summary["timer"] = None
        irc._RECONNECT_THAW_QUIET_SECONDS = self._real_quiet_seconds
        super().tearDown()

    def wait_for_flush(self, timeout=1.0):
        deadline = time.time() + timeout
        while time.time() < deadline and not self.captured:
            time.sleep(0.01)

    def test_a_single_channel_still_gets_one_line(self):
        irc._note_reconnect_thaw("#one", 3)
        self.wait_for_flush()

        self.assertEqual(len(self.captured), 1)
        category, text = self.captured[0]
        self.assertEqual(category, "JOIN")
        self.assertIn("3", text)
        self.assertIn("1 channel", text)
        self.assertNotIn("1 channels", text, "one channel is not plural")

    def test_several_channels_in_one_burst_collapse_into_one_line(self):
        """The actual incident, at a smaller scale: five channels' worth of
        NAMES-sync thaws, all inside the same burst, must cost one debug
        line - not five."""
        irc._note_reconnect_thaw("#one", 2)
        irc._note_reconnect_thaw("#two", 1)
        irc._note_reconnect_thaw("#three", 4)
        irc._note_reconnect_thaw("#four", 0)  # a channel that thawed nobody
        irc._note_reconnect_thaw("#five", 3)
        self.wait_for_flush()

        self.assertEqual(len(self.captured), 1,
                         "one burst of channels must cost one debug line")
        _, text = self.captured[0]
        self.assertIn("10", text)  # 2 + 1 + 4 + 0 + 3
        self.assertIn("5 channels", text)

    def test_a_channel_with_nobody_thawed_does_not_start_a_summary(self):
        irc._note_reconnect_thaw("#empty", 0)
        time.sleep(irc._RECONNECT_THAW_QUIET_SECONDS * 3)

        self.assertEqual(self.captured, [],
                         "a channel that thawed nobody has nothing worth reporting")

    def test_two_separate_bursts_each_get_their_own_line(self):
        """Debounced, not deduplicated forever: a second reconnect later on
        must be reported on its own, not silently merged into the first."""
        irc._note_reconnect_thaw("#one", 2)
        self.wait_for_flush()
        self.assertEqual(len(self.captured), 1)

        irc._note_reconnect_thaw("#one", 5)
        self.wait_for_flush(timeout=1.0)
        deadline = time.time() + 1.0
        while time.time() < deadline and len(self.captured) < 2:
            time.sleep(0.01)

        self.assertEqual(len(self.captured), 2)
        self.assertIn("5", self.captured[1][1])

    def test_a_late_channel_restarts_the_quiet_window(self):
        """The debounce must reset on every new arrival, so the summary
        fires shortly after the LAST channel in the burst settles - not a
        fixed time after the first, which would still split a slow burst
        into two lines."""
        irc._note_reconnect_thaw("#one", 1)
        time.sleep(irc._RECONNECT_THAW_QUIET_SECONDS * 0.6)
        self.assertEqual(self.captured, [],
                         "must not have flushed yet - a second channel is still due")
        irc._note_reconnect_thaw("#two", 1)

        self.wait_for_flush()
        self.assertEqual(len(self.captured), 1,
                         "the late arrival must have joined the first, not split off")
        self.assertIn("2 channels", self.captured[0][1])

    def test_the_353_handler_actually_calls_the_batched_helper(self):
        """A source-text check, not an executed one - the NAMES-sync handler
        lives inside irc_loop(), a single function reading a live socket
        that this suite does not run line-by-line. Weaker than executing it,
        but real: every test above happily proves the helper batches
        correctly even if the call site quietly went back to calling
        send_debug() directly, so something has to check the wiring too."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo_root, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn(
            "_note_reconnect_thaw(chan, len(thawed_users))", source,
            "the NAMES-sync handler must call the batching helper, not "
            "send_debug() directly - see #406")


if __name__ == "__main__":
    unittest.main()
