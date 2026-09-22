"""#577 and #602: a DCC RESUME blocked the IRC read thread.

The ACCEPT goes out through the shared pacer, which sleeps until a slot is
free - up to MSG_DELAY (5 s by default). handle_resume_request() ran on the
read thread, so every matching RESUME cost that long with no PING answered and
no other line parsed; a peer that sent them in a loop could starve the loop
until the server dropped the bot, and because the flood counter stamps a
request when it is handled, the stall spaced the lines out just enough that the
sender was never muted.

The lookup and the resume position stay on the read thread (they must be
settled before the receiver can connect); the paced send is a short thread,
one at a time per offer, and a RESUME that arrives while one is waiting only
updates the position the reply will carry.
"""

import os
import sys
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_dcc_resume import FakeIrcSocket, OFFERED, PORT, SIZE, USER  # noqa: E402


class WhileThePacerIsBusy(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        self.sock = FakeIrcSocket()
        dcc.register_send_offer(USER, PORT, OFFERED, SIZE)
        # The pacer holds every send until the test lets it go.
        self.release = threading.Event()
        self.entered = threading.Event()
        self.inside = 0            # helpers currently parked in the pacer stub
        self.inside_lock = threading.Lock()

        def held(_interval):
            with self.inside_lock:
                self.inside += 1
            try:
                self.entered.set()
                self.release.wait(10)
            finally:
                with self.inside_lock:
                    self.inside -= 1

        patch = mock.patch.object(runtime.outbound_pacer, "wait_for_slot", held)
        patch.start()
        # Release, THEN wait for every helper to leave the pacer before the
        # next test starts. The offer lives in the module-global
        # runtime.dcc_send_offers under the same (USER, PORT) in every test
        # here, so a helper still parked from the previous test woke up in
        # the next one, popped accept_pending off the NEW offer, and the
        # third RESUME started a second helper: "2 != 1" on a slow runner
        # (#778). Cleanups run last-registered first, so this one runs
        # before patch.stop().
        self.addCleanup(patch.stop)
        self.addCleanup(self.wait_for_the_helpers_to_leave)
        self.addCleanup(self.release.set)

    def wait_for_the_helpers_to_leave(self):
        end = time.time() + 5
        while time.time() < end:
            with self.inside_lock:
                if self.inside == 0:
                    return
            time.sleep(0.01)

    def resume(self, position, **kw):
        return dcc.handle_resume_request(
            self.sock, USER, f"DCC RESUME {OFFERED} {PORT} {position}", **kw)

    def wait_for_sends(self, count):
        end = time.time() + 5
        while len(self.sock.sent) < count and time.time() < end:
            time.sleep(0.01)

    def test_the_read_thread_is_not_held_by_the_pacer(self):
        started = time.monotonic()
        answered = self.resume(4096, background=True)
        took = time.monotonic() - started

        self.assertTrue(answered)
        self.assertLess(took, 0.5, "the caller waited for the pacer")
        self.assertTrue(self.entered.wait(5), "the reply never reached the pacer")
        self.assertEqual(self.sock.sent, [], "sent before the pacer allowed it")

    def test_the_position_is_settled_before_the_call_returns(self):
        """A receiver connects once it has the ACCEPT; the sending thread reads
        the offset then. It must already be there."""
        self.resume(4096, background=True)
        self.assertEqual(runtime.dcc_send_offers[(USER, PORT)]["position"], 4096)

    def test_the_accept_arrives_once_the_pacer_allows_it(self):
        self.resume(4096, background=True)
        self.release.set()
        self.wait_for_sends(1)
        self.assertEqual(len(self.sock.sent), 1)
        self.assertIn(f"\x01DCC ACCEPT {OFFERED} {PORT} 4096\x01", self.sock.sent[0])

    def test_a_second_resume_while_one_waits_starts_no_second_reply(self):
        starts = []
        real_thread = threading.Thread

        class Counting(real_thread):
            def start(self_inner):
                if getattr(self_inner, "_target", None) is dcc._send_resume_accept:
                    starts.append(1)
                super().start()

        with mock.patch.object(dcc.threading, "Thread", Counting):
            for position in (1000, 2000, 3000):
                self.assertTrue(self.resume(position, background=True))
        self.assertEqual(len(starts), 1)

    def test_the_reply_carries_the_latest_position(self):
        self.resume(1000, background=True)
        self.assertTrue(self.entered.wait(5))
        self.resume(2000, background=True)
        self.resume(3000, background=True)
        self.release.set()
        self.wait_for_sends(1)
        time.sleep(0.2)
        self.assertEqual(len(self.sock.sent), 1)
        self.assertIn(f"{PORT} 3000\x01", self.sock.sent[0])

    def test_the_offer_is_free_for_the_next_resume_afterwards(self):
        self.resume(1000, background=True)
        self.release.set()
        self.wait_for_sends(1)
        end = time.time() + 5
        while runtime.dcc_send_offers[(USER, PORT)].get("accept_pending") and time.time() < end:
            time.sleep(0.01)
        self.assertNotIn("accept_pending", runtime.dcc_send_offers[(USER, PORT)])
        self.resume(2000, background=True)
        self.wait_for_sends(2)
        self.assertIn(f"{PORT} 2000\x01", self.sock.sent[-1])

    def test_a_line_that_matches_no_offer_costs_nothing_and_starts_nothing(self):
        started = time.monotonic()
        answered = dcc.handle_resume_request(
            self.sock, USER, f"DCC RESUME {OFFERED} {PORT + 1} 10", background=True)
        self.assertFalse(answered)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.entered.is_set())

    def test_an_offer_that_is_gone_by_the_time_the_slot_comes_gets_no_reply(self):
        self.resume(4096, background=True)
        self.assertTrue(self.entered.wait(5))
        with runtime.dcc_send_offers_lock:
            runtime.dcc_send_offers.pop((USER, PORT), None)
        self.release.set()
        time.sleep(0.3)
        self.assertEqual(self.sock.sent, [])

    def test_a_thread_that_cannot_start_leaves_the_offer_answerable(self):
        with mock.patch.object(dcc.threading, "Thread", side_effect=RuntimeError("no threads")):
            self.assertFalse(self.resume(4096, background=True))
        self.assertNotIn("accept_pending", runtime.dcc_send_offers[(USER, PORT)])


class TheReadLoop(unittest.TestCase):

    def test_the_read_loop_asks_for_the_background_reply(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("dcc.handle_resume_request(")
        call = source[start:start + 200]
        self.assertIn("background=True", call)


class TheDefaultIsUnchanged(DCCoreTestCase):
    """Everything else that calls it (and every existing test) still gets the
    reply on the calling thread, before it returns."""

    def test_without_background_the_accept_is_sent_before_returning(self):
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        sock = FakeIrcSocket()
        dcc.register_send_offer(USER, PORT, OFFERED, SIZE)
        self.assertTrue(dcc.handle_resume_request(sock, USER, f"DCC RESUME {OFFERED} {PORT} 10"))
        self.assertEqual(len(sock.sent), 1)


if __name__ == "__main__":
    unittest.main()
