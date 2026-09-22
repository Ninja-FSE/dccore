"""The outbound pump wrote from the moment of TCP connect (audit M28, #630).

irc.py publishes oserve.irc_connection straight after connect(), before
NICK/USER have gone out and seconds before the JOINs land, and
queue_mgr.queue_worker's only gate was "is there a socket". So whatever the
last connection left queued - a "Sent:" notice, two queue positions, a
rejoin - drained into a window the server answers with 451 (unregistered)
and 404 (not in channel), and the lines were gone with no log line. The
debug drain in announce.py has had a second gate for exactly this reason.

THE GATE IS ACTIVATION, NOT CHANNEL SYNC. config.bot_joined_channel stays
False on a connection that never got into a channel (banned, invite-only,
misspelled), and the JOIN that asks to be let back in goes through this very
pump - so gating on the sync flag would hold the retry for ever.
config.activation_triggered is set once every target channel has answered
its JOIN, or the watchdog has given up waiting, and is cleared by the
disconnect epilogue.

AND THE STALE VIP BACKLOG IS NOW ACTUALLY CLEARED. The epilogue emptied
send_queue["channel_announce"], a key oserve.queue_message() never writes -
adverts go to vip_queue - so it cleared nothing.
"""

import contextlib
import io
import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import queue_mgr  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_a_shared_outbound_pace import _SleepShim, TimestampedSocket  # noqa: E402


class _CountingShim(_SleepShim):
    """Counts the worker's hold sleeps, so a test can wait until the loop has
    demonstrably been round the gate several times rather than sleeping a
    fixed span and hoping it had."""

    def __init__(self, cap=0.005):
        super().__init__(cap)
        self.passes = 0
        self.passed = threading.Condition()

    def sleep(self, seconds):
        with self.passed:
            self.passes += 1
            self.passed.notify_all()
        super().sleep(seconds)

    def wait_for_passes(self, count, timeout=3.0):
        deadline = time.monotonic() + timeout
        with self.passed:
            while self.passes < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.passed.wait(remaining)
        return True


class TheBacklogWaitsForActivation(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(MSG_DELAY=0.01)
        config.vip_queue = []
        config.send_queue = {}
        # The window under test: a socket, and nothing else yet.
        config.activation_triggered = False
        config.bot_joined_channel = False
        self.oserve.bot_joined_channel = False
        self.sock = TimestampedSocket()
        self.oserve.irc_connection = self.sock
        self._real_pacer = runtime.outbound_pacer
        runtime.outbound_pacer = runtime.OutboundPacer()
        self._real_time = queue_mgr.time
        self.shim = _CountingShim()
        queue_mgr.time = self.shim
        self.thread = None
        config.vip_queue.append("NOTICE somebody :Sent: somefile.rar\r\n")
        config.vip_queue.append("JOIN #somechannel\r\n")
        config.send_queue["another"] = ["NOTICE another :You are 2nd in the queue\r\n"]

    def tearDown(self):
        self.shim.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3.0)
            self.assertFalse(self.thread.is_alive(), "the pump thread must be gone")
        queue_mgr.time = self._real_time
        runtime.outbound_pacer = self._real_pacer
        super().tearDown()

    def start_worker(self):
        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    queue_mgr.queue_worker()
                except SystemExit:
                    pass
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def sent(self):
        return [payload.decode("utf-8") for _t, payload in self.sock.sent]

    def wait_for(self, predicate, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_nothing_goes_out_on_a_socket_that_is_not_registered_yet(self):
        self.start_worker()

        self.assertTrue(self.shim.wait_for_passes(5), "the worker never reached its hold")
        self.assertEqual(self.sent(), [])
        self.assertEqual(len(config.vip_queue), 2, "the VIP lane must be held, not dropped")
        self.assertEqual(config.send_queue["another"],
                         ["NOTICE another :You are 2nd in the queue\r\n"])

    def test_and_everything_goes_out_once_the_joins_have_landed(self):
        self.start_worker()
        self.assertTrue(self.shim.wait_for_passes(3))

        config.activation_triggered = True

        self.assertTrue(self.wait_for(lambda: len(self.sock.sent) == 3),
                        "expected all three lines, got %r" % self.sent())
        self.assertEqual(self.sent()[0], "NOTICE somebody :Sent: somefile.rar\r\n")

    def test_the_gate_is_not_the_channel_sync_flag(self):
        """A connection that never got into a channel has bot_joined_channel
        False for its whole life, and the rejoin JOIN goes through this pump.
        Held on that flag, it would never ask to be let back in."""
        config.activation_triggered = True
        config.bot_joined_channel = False

        self.start_worker()

        self.assertTrue(self.wait_for(lambda: "JOIN #somechannel\r\n" in self.sent()),
                        "the rejoin never went out: %r" % self.sent())

    def test_a_disconnect_closes_the_gate_again(self):
        """activation_triggered is per connection: the epilogue clears it, so
        the next connection's pre-registration window is closed too."""
        config.activation_triggered = True
        self.start_worker()
        self.assertTrue(self.wait_for(lambda: len(self.sock.sent) == 3))

        config.activation_triggered = False
        config.vip_queue.append("PRIVMSG #somechannel :a late advert\r\n")
        before = self.shim.passes
        self.assertTrue(self.shim.wait_for_passes(before + 5))

        self.assertEqual(len(self.sock.sent), 3)
        self.assertEqual(config.vip_queue, ["PRIVMSG #somechannel :a late advert\r\n"])


class TheEpilogueClearsTheLaneAdvertsActuallyUse(unittest.TestCase):
    """The epilogue lives inside irc_loop(), which this suite does not run
    line-by-line (see test_reconnect.py), so this reads the text: the dead
    key is gone and the live lane is what gets emptied."""

    def epilogue(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()
        return source.split("Lost the connection. Reconnecting", 1)[1][:2500]

    def test_the_vip_lane_is_emptied(self):
        self.assertIn("del config.vip_queue[:]", self.epilogue())

    def test_the_key_nothing_writes_is_no_longer_what_is_cleared(self):
        """The statement, not the name - the name survives in the comment
        that explains what was wrong with it."""
        self.assertNotIn('send_queue["channel_announce"] = []', self.epilogue())

    def test_adverts_really_do_go_to_the_vip_lane(self):
        """The premise of both checks above, executed: queue_message() puts
        a channel advert in vip_queue, never under a send_queue key. The real
        module, loaded the way test_the_search_header_goes_first.py does -
        the harness's stub only records what it is handed."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "oserve_under_test", os.path.join(REPO_ROOT, "oserve.py"))
        oserve = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oserve)
        saved_vip, saved_send = list(config.vip_queue), dict(config.send_queue)
        try:
            config.vip_queue[:] = []
            config.send_queue.clear()
            oserve.queue_message("channel_announce", "PRIVMSG #somechannel :advert\r\n")

            self.assertEqual(config.vip_queue, ["PRIVMSG #somechannel :advert\r\n"])
            self.assertNotIn("channel_announce", config.send_queue)
        finally:
            config.vip_queue[:] = saved_vip
            config.send_queue.clear()
            config.send_queue.update(saved_send)


if __name__ == "__main__":
    unittest.main()
