"""The DCC ACCEPT reply went straight onto the IRC socket.

Every other thing the bot says waits for a slot on `runtime.outbound_pacer` -
the shared clock that exists because this bot has been disconnected with
"Excess Flood" in production. `!ping` was one exception and is fixed; the
`DCC ACCEPT` that answers a peer's resume request is the other (#453).

It is not queued behind the round-robin. A resume handshake is something a
peer is actively waiting on, so it stays immediate; what changes is that it
takes a slot on the same clock instead of ignoring it. The wait is only paid
when the bot has just sent something else, and is at most MSG_DELAY.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheResumeHandshakeTakesItsTurn(unittest.TestCase):
    """Read from the source: reaching the real function means a peer, a socket
    and a partly-transferred file, and the property at issue is which call
    comes first rather than any of that."""

    @staticmethod
    def accept_block():
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as f:
            body = f.read()
        block = body.split("DCC ACCEPT ", 1)[1]
        return block.split("[DCC-RESUME] Could not answer", 1)[0]

    def test_the_accept_waits_for_a_slot(self):
        self.assertIn("runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)",
                      self.accept_block())

    def test_the_slot_is_taken_before_the_write_and_not_after(self):
        """Ordering is the whole property. A slot taken after the send paces
        the NEXT line and lets this one out unmetered, which is the bug."""
        block = self.accept_block()

        self.assertLess(block.index("wait_for_slot"),
                        block.index("irc_sock.sendall(reply.encode("))


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
