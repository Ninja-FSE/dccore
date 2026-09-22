"""A receiver that acked a bogus high total was credited with a completed
transfer (audit M54, #656).

_AckTracker accepted any 32-bit word above what it held, with no upper bound
tied to what had actually been sent, and _wait_for_final_ack() only tested
acked >= file_size. So one word of 0xFFFFFFFF from a peer that read nothing
satisfied completion for any file under 4 GB: "Sent:" announced in the
channel, Files/bytes totals and the most-downloaded counter incremented, the
queue row consumed, at no bandwidth cost - and repeated in a loop, the
public stats inflated. A legitimate client acking in the wrong byte order
(4096 -> 0x00100000 = 1 MB) was declared complete after its first packet
and cut off.

The send loop now keeps the tracker told what has been handed to the
kernel, and a word past that is not a position the receiver can hold: it is
ignored and counted. The transfer then lives or dies on the real acks like
any other - a peer that sends only bogus words stalls and fails.
"""

import os
import struct
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402

from tests.test_complete_means_the_receiver_acked_it import ARealReceiver, CONTENT  # noqa: E402


class TheAckCannotExceedWhatWasSent(unittest.TestCase):

    def test_a_word_past_what_was_sent_is_ignored(self):
        t = dcc._AckTracker()
        t.sent = 4096
        t.feed(struct.pack("!I", 0xFFFFFFFF))

        self.assertEqual(t.acked, 0)
        self.assertEqual(t.overshoots, 1)

    def test_and_does_not_reset_the_stall_clock(self):
        """A bogus word is not progress."""
        t = dcc._AckTracker()
        t.sent = 4096
        t.last_advance_at = time.time() - dcc.ACK_STALL_SECONDS - 1
        t.feed(struct.pack("!I", 1 << 20))

        self.assertTrue(t.stalled())

    def test_the_wrong_byte_order_is_caught_the_same_way(self):
        """4096 acked little-endian reads as 0x00100000 = 1 MB."""
        t = dcc._AckTracker()
        t.sent = 4096
        t.feed(struct.pack("<I", 4096))

        self.assertEqual(t.acked, 0)

    def test_exactly_what_was_sent_is_the_ceiling_not_below_it(self):
        t = dcc._AckTracker()
        t.sent = 4096
        t.feed(struct.pack("!I", 4096))

        self.assertEqual(t.acked, 4096)

    def test_the_ceiling_moves_with_the_send(self):
        t = dcc._AckTracker()
        t.sent = 4096
        t.feed(struct.pack("!I", 8192))         # too early: ignored
        self.assertEqual(t.acked, 0)
        t.sent = 8192
        t.feed(struct.pack("!I", 8192))         # now it is a real position
        self.assertEqual(t.acked, 8192)

    def test_the_wrap_past_4_gb_still_works_under_the_ceiling(self):
        t = dcc._AckTracker(start=(1 << 32) - 1000)
        t.sent = (1 << 32) + 100
        t.feed(struct.pack("!I", 100))
        self.assertEqual(t.acked, (1 << 32) + 100)


class ABogusAckOverLoopback(ARealReceiver):
    """The audit's probe, as a test: a peer that writes one word of
    0xFFFFFFFF, reads nothing, and keeps the socket open."""

    def test_is_a_failure_not_a_sale(self):
        irc, sender = self.start_send()
        client = self.connect(irc.port())
        client.sendall(struct.pack("!I", 0xFFFFFFFF))

        sender.join(30)

        self.assertFalse(sender.is_alive())
        self.assertEqual(self.sent_lines(), [], "a peer that read nothing was credited with the file")
        self.assertEqual(len(self.failed_lines()), 1, self.debug_lines)
        # (total_sent_bytes counts what went into the kernel, and on loopback
        # the whole file does; the counters that mean "sold" are the Sent:
        # line and the statistics, and neither moved.)

    def test_a_real_receiver_that_also_sends_one_bogus_word_still_completes(self):
        """The cap must not cost an honest client: a stray high word among
        real acks is ignored, and the real ones carry the transfer."""
        irc, sender = self.start_send()
        client = self.connect(irc.port())
        client.sendall(struct.pack("!I", 0xFFFFFFFF))
        got = self.receive(client)
        sender.join(30)

        self.assertEqual(got, CONTENT)
        self.assertEqual(len(self.sent_lines()), 1, self.debug_lines)
        self.assertEqual(self.failed_lines(), [])


# The inherited cases already ran in their own module; here only the bogus-ack ones.
for _name in [n for n in dir(ARealReceiver) if n.startswith("test")]:
    setattr(ABogusAckOverLoopback, _name, None)


class TheSendLoopKeepsTheTrackerTold(unittest.TestCase):
    """The ceiling is only as good as its upkeep: the line after every
    sendall() that moves bytes_sent moves the tracker's `sent` with it."""

    def test_sent_is_updated_beside_bytes_sent(self):
        import inspect
        body = inspect.getsource(dcc.start_dcc_send)
        loop = body.split("conn.sendall(chunk)", 1)[1][:200]

        self.assertIn("bytes_sent += len(chunk)", loop)
        self.assertIn("acks.sent = bytes_sent", loop)


if __name__ == "__main__":
    unittest.main()
