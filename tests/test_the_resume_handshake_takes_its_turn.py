"""The DCC ACCEPT reply went straight onto the IRC socket.

Every other thing the bot says waits for a slot on `runtime.outbound_pacer` -
the shared clock that exists because this bot has been disconnected with
"Excess Flood" in production. `!ping` was one exception and is fixed; the
`DCC ACCEPT` that answers a peer's resume request is the other (#453).

It is not queued behind the round-robin. A resume handshake is something a
peer is actively waiting on, so it stays immediate; what changes is that it
takes a slot on the same clock instead of ignoring it. The wait is only paid
when the bot has just sent something else, and is at most MSG_DELAY.

DRIVEN, NOT READ (#645, audit M43). This used to read dcc.py for the string
"wait_for_slot" before "irc_sock.sendall(reply.encode(" in the function's
text - which a comment satisfies, and which stayed green with the call
commented out or moved into `if False:` (the audit checked all three). The
function needs only a runtime.dcc_send_offers entry and an object with
.sendall(), and test_complete_means_the_receiver_acked_it.py was already
calling it for real. So: a spy in place of the pacer, a recording socket,
and the ORDER of the two calls read off one shared log.
"""

import contextlib
import io
import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

PEER = "somepeer"
PORT = 4321
FILE = "Some_Album.rar"
SIZE = 10_000


class _SpyPacer:
    """Records every slot asked for, in a log it shares with the socket."""

    def __init__(self, log):
        self.log = log

    def wait_for_slot(self, min_interval):
        self.log.append(("wait_for_slot", float(min_interval)))


class _SpySocket:
    def __init__(self, log):
        self.log = log

    def sendall(self, payload):
        self.log.append(("sendall", payload.decode("utf-8", "replace")))


class AnsweringAResume(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.log = []
        self._real_pacer = runtime.outbound_pacer
        runtime.outbound_pacer = _SpyPacer(self.log)
        self.addCleanup(setattr, runtime, "outbound_pacer", self._real_pacer)
        self.set_config(MSG_DELAY=5.0)
        real_feed = announce.feed_event
        announce.feed_event = lambda *a, **k: None
        self.addCleanup(setattr, announce, "feed_event", real_feed)
        with runtime.dcc_send_offers_lock:
            runtime.dcc_send_offers[(PEER, PORT)] = {"filename": FILE, "size": SIZE}
        self.sock = _SpySocket(self.log)

    def resume(self, position=500, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return dcc.handle_resume_request(
                self.sock, PEER.capitalize(), f"DCC RESUME {FILE} {PORT} {position}", **kwargs)

    def calls(self):
        return [name for name, _arg in self.log]

    def test_the_accept_goes_out(self):
        self.assertTrue(self.resume())

        self.assertIn("sendall", self.calls())
        self.assertIn(f"DCC ACCEPT {FILE} {PORT} 500", self.log[-1][1])

    def test_it_waits_for_a_slot_on_the_shared_clock(self):
        self.resume()

        self.assertIn(("wait_for_slot", 5.0), self.log, "the ACCEPT did not take a slot on the pacer")

    def test_the_slot_is_taken_before_the_write_and_not_after(self):
        """Ordering is the whole property. A slot taken after the send paces
        the NEXT line and lets this one out unmetered, which is the bug."""
        self.resume()

        self.assertEqual(self.calls(), ["wait_for_slot", "sendall"])

    def test_the_wait_is_msg_delay_not_a_number_of_its_own(self):
        self.set_config(MSG_DELAY=1.25)
        self.resume()

        self.assertEqual([arg for name, arg in self.log if name == "wait_for_slot"], [1.25])

    def test_a_stray_resume_for_no_offer_of_ours_touches_neither(self):
        """Anyone on the network can send DCC RESUME. One that matches no
        port we are listening on for that nick is not answered - and takes
        no slot either; the clock is for lines that go out."""
        answered = dcc.handle_resume_request(self.sock, "stranger", f"DCC RESUME {FILE} 9999 1")

        self.assertFalse(answered)
        self.assertEqual(self.log, [])

    def test_from_the_read_thread_the_same_order_holds_on_the_helper_thread(self):
        """`background=True` is what the IRC read loop passes (#577, #602):
        the lookup stays on the read thread and the paced send moves to a
        short-lived thread. Same two calls, same order, just elsewhere."""
        done = threading.Event()
        real_send = self.sock.sendall

        def send_then_flag(payload):
            real_send(payload)
            done.set()
        self.sock.sendall = send_then_flag

        self.assertTrue(self.resume(background=True))

        self.assertTrue(done.wait(5), "the ACCEPT never went out from the helper thread")
        self.assertEqual(self.calls(), ["wait_for_slot", "sendall"])


class EveryTestGetsItsOwnClock(DCCoreTestCase):
    """runtime.outbound_pacer is a process-wide singleton holding "the
    earliest moment the next line may leave"."""

    def test_the_clock_does_not_carry_over_from_another_test(self):
        """Without the reset, a test that sent anything left the next one's
        first send blocked for up to MSG_DELAY - five seconds by default -
        which is leaked state, and wall-clock every suite run pays."""
        runtime.outbound_pacer.wait_for_slot(3600)
        reserved = runtime.outbound_pacer._next_allowed

        self.setUp()

        self.assertLess(runtime.outbound_pacer._next_allowed, reserved,
                        "this test inherited the previous reservation")

    def test_a_fresh_clock_lets_the_first_send_through_at_once(self):
        import time

        started = time.monotonic()
        runtime.outbound_pacer.wait_for_slot(5.0)

        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
