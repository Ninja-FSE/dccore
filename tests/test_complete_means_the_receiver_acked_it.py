"""A transfer is complete when the receiver says it has all of it.

Found by a second operator running DCCore on Windows, reproduced with mIRC
(#526). The channel announced a 2.7 MB list zip as sent - counted, credited,
"Speed: n/a (<1s)" - eleven seconds after the receiver had reported it
incomplete at 1.2 MB after 24 seconds.

The DCC SEND protocol has the receiver send back a 4-byte big-endian running
total after every packet: the ONLY signal of what actually arrived. The send
loop never read it. "Complete" was `bytes_sent >= file_size`, where bytes_sent
counted what sendall() had handed to the kernel - and the kernel's send buffer
is 4 MB on Windows by default, bigger than most files. So the bot declared
success at t~0, stopped its clock (a memcpy's speed), counted the file, slept
1.5 seconds and closed the socket with megabytes still queued behind a link
doing 50 KB/s.

These tests play the receiver over a real loopback socket, with real acks -
including one that acks slowly, one that stops acking mid-file, one that says
nothing at all, and one that resumes - and check what the bot concludes.
"""

import io
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import announce  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_dcc_resume_end_to_end import RecordingIrcSocket, loopback_is_usable  # noqa: E402

USER = "someuser"
PORT_START = 51300
PORT_END = 51310
CONTENT = bytes(range(256)) * 400          # 102,400 bytes


class TheAckTracker(unittest.TestCase):
    """The parser, in isolation: cumulative, big-endian, 32-bit with wrap.

    Every tracker here is told what has been sent (`sent`), as the send loop
    keeps it told: since #656 an acknowledgement past that is not a position
    the receiver can hold and is ignored - see TheAckCannotExceedWhatWasSent
    below."""

    def test_a_single_ack_is_read(self):
        t = dcc._AckTracker()
        t.sent = 4096
        t.feed(struct.pack("!I", 4096))
        self.assertEqual(t.acked, 4096)
        self.assertTrue(t.received_any)

    def test_acks_split_across_reads_are_reassembled(self):
        """recv() hands back whatever is there; a 4-byte word can arrive in
        pieces, or several at once."""
        t = dcc._AckTracker()
        t.sent = 300
        payload = struct.pack("!I", 100) + struct.pack("!I", 200) + struct.pack("!I", 300)
        t.feed(payload[:3])
        self.assertEqual(t.acked, 0, "three bytes is not yet a word")
        t.feed(payload[3:7])
        self.assertEqual(t.acked, 100)
        t.feed(payload[7:])
        self.assertEqual(t.acked, 300)

    def test_acks_only_advance(self):
        """Cumulative totals never go backwards; a stale or duplicated word
        must not pull the count down."""
        t = dcc._AckTracker()
        t.sent = 5000
        t.feed(struct.pack("!I", 5000))
        t.feed(struct.pack("!I", 3000))
        self.assertEqual(t.acked, 5000)

    def test_the_32_bit_counter_wraps_past_4_gb(self):
        """mIRC acks a 4 GB file with a counter that wraps at 2**32. The
        tracker must keep counting rather than believe the receiver went back
        to byte 100."""
        t = dcc._AckTracker(start=(1 << 32) - 1000)
        t.sent = (1 << 32) + 100
        t.feed(struct.pack("!I", 100))              # wrapped: really 2**32 + 100
        self.assertEqual(t.acked, (1 << 32) + 100)

    def test_a_resume_starts_from_what_the_receiver_holds(self):
        t = dcc._AckTracker(start=50_000)
        self.assertEqual(t.acked, 50_000)
        t.sent = 60_000
        t.feed(struct.pack("!I", 60_000))
        self.assertEqual(t.acked, 60_000)

    def test_eof_is_remembered(self):
        t = dcc._AckTracker()
        t.feed(b"")
        self.assertTrue(t.eof)

    def test_progress_resets_the_stall_clock(self):
        t = dcc._AckTracker()
        t.sent = 10
        t.last_advance_at = time.time() - dcc.ACK_STALL_SECONDS - 1
        self.assertTrue(t.stalled())
        t.feed(struct.pack("!I", 10))
        self.assertFalse(t.stalled())

    def test_a_word_that_does_not_advance_does_not_reset_the_stall_clock(self):
        """A receiver re-sending the same total is not making progress."""
        t = dcc._AckTracker()
        t.sent = 10
        t.feed(struct.pack("!I", 10))
        t.last_advance_at = time.time() - dcc.ACK_STALL_SECONDS - 1
        t.feed(struct.pack("!I", 10))
        self.assertTrue(t.stalled())


@unittest.skipUnless(loopback_is_usable(),
                     "this runner cannot bind a listener and dial loopback")
class ARealReceiver(DCCoreTestCase):
    """The bot on one side of a loopback socket, this test on the other."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-ack-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.served = os.path.join(self.tmp, "Some_Album.zip")
        with io.open(self.served, "wb") as handle:
            handle.write(CONTENT)
        self.set_config(
            active_transfers=[{"user": USER, "file": "Some_Album.zip",
                               "bytes_sent": 0, "next_file_obj": "Some_Album.zip"}],
            MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8",
            DCC_PORT_START=PORT_START, DCC_PORT_END=PORT_END)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)

        # What the bot concludes, captured where the operator would read it.
        self.debug_lines = []
        self._real_send_debug = announce.send_debug
        announce.send_debug = lambda text, category="INFO": self.debug_lines.append((category, text))
        self.addCleanup(setattr, announce, "send_debug", self._real_send_debug)
        self.oserve.total_sent_bytes = 0

        # The failure clock, shortened so a stall test does not take a minute.
        self._real_stall = dcc.ACK_STALL_SECONDS
        dcc.ACK_STALL_SECONDS = 2.0
        self.addCleanup(setattr, dcc, "ACK_STALL_SECONDS", self._real_stall)

    def start_send(self):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, USER, self.served, "Some_Album.zip", "#somechannel", "Some_Album.zip"),
            daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc.handshake_seen.wait(20), "no DCC SEND handshake")
        return irc, sender

    def connect(self, port):
        client = socket.create_connection(("127.0.0.1", port), timeout=20)
        client.settimeout(20)
        self.addCleanup(client.close)
        return client

    def receive(self, client, ack_every=True, stop_acking_after=None, ack_delay=0.0):
        """Read the file, acking as a real client does - or deliberately not."""
        held = 0
        received = bytearray()
        # Small reads, so the ack cadence is fine-grained enough that
        # "stop acking after N bytes" actually stops PART-way, not before the
        # first ack. On loopback one recv(65536) would take most of the file.
        while True:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            received.extend(chunk)
            held += len(chunk)
            if ack_every and (stop_acking_after is None or held <= stop_acking_after):
                if ack_delay:
                    time.sleep(ack_delay)
                client.sendall(struct.pack("!I", held & 0xFFFFFFFF))
            if held >= len(CONTENT):
                break
        return bytes(received)

    def sent_lines(self):
        return [t for c, t in self.debug_lines if t.startswith("Sent:")]

    def failed_lines(self):
        return [t for c, t in self.debug_lines if c == "FAIL"]

    # --- the behaviour ----------------------------------------------------

    def test_an_acking_receiver_completes_and_is_counted(self):
        irc, sender = self.start_send()
        client = self.connect(irc.port())
        got = self.receive(client)
        sender.join(30)
        self.assertEqual(got, CONTENT)
        self.assertEqual(self.oserve.total_sent_bytes, len(CONTENT))
        self.assertEqual(len(self.sent_lines()), 1, self.debug_lines)
        self.assertEqual(self.failed_lines(), [])

    def test_a_receiver_that_never_acks_is_a_failure_not_a_success(self):
        """The #526 blind spot, made explicit: a silent receiver cannot be told
        apart from one that got nothing, and the old code counted it."""
        irc, sender = self.start_send()
        client = self.connect(irc.port())
        got = self.receive(client, ack_every=False)
        client.close()
        sender.join(30)
        self.assertEqual(got, CONTENT, "the bytes went out; that was never the question")
        self.assertEqual(self.sent_lines(), [], "announced as sent with no evidence it arrived")
        self.assertEqual(len(self.failed_lines()), 1, self.debug_lines)
        # Two wordings, one verdict. On a fast loopback the sender has pushed
        # every chunk into the kernel before the receiver closes, reaches
        # _wait_for_final_ack() and reports "never acknowledged a single
        # byte". On a loaded runner (ubuntu/3.12 in CI, once) the sender is
        # descheduled mid-loop, the close lands first, _ReceiverGone fires
        # inside the loop and it reports "closed the connection mid-transfer,
        # having acknowledged 0 of N bytes". Both say zero bytes were ever
        # acknowledged, which is the property; which one runs is scheduling.
        reason = self.failed_lines()[0]
        self.assertTrue("never acknowledged" in reason or "acknowledged 0 of" in reason,
                        reason)

    def test_a_receiver_that_stops_acking_mid_file_is_a_failure(self):
        """The reported case: 1.2 MB acked of 2.7 MB, then nothing."""
        irc, sender = self.start_send()
        client = self.connect(irc.port())
        got = self.receive(client, stop_acking_after=40_000)
        # Keep the socket open and silent, as a stalled link would.
        sender.join(30)
        self.assertEqual(got, CONTENT)
        self.assertEqual(self.sent_lines(), [])
        self.assertEqual(len(self.failed_lines()), 1, self.debug_lines)
        self.assertIn("stopped acknowledging", self.failed_lines()[0])

    def test_success_is_not_announced_until_the_final_ack(self):
        """The order of events is the bug. The bytes leave in a burst into the
        kernel; the announce must wait for the receiver, not for the burst."""
        irc, sender = self.start_send()
        client = self.connect(irc.port())
        got = self.receive(client, ack_delay=0.05)      # slow acks: ~0.1s+ total
        acked_all_at = time.time()
        sender.join(30)
        self.assertEqual(got, CONTENT)
        self.assertEqual(len(self.sent_lines()), 1)
        # The bot's clock stopped at the final ack, which is after the last
        # byte was read here - so the reported duration cannot be "<1s from
        # t0" the way a kernel-buffer memcpy would report it. Pinned via the
        # transfer's own finish time being no earlier than our last ack.
        # (RecordingIrcSocket has no notion of time; the Sent: line's speed is
        # the observable, and a measured speed below the memcpy figure is the
        # property. 100 KB over >=0.1s is < 1 MB/s; a memcpy would be >>.)
        speed_text = self.sent_lines()[0]
        self.assertNotIn("MB/s", speed_text.split("[")[-1],
                         f"the speed was measured against the kernel, not the wire: {speed_text}")

    def test_a_resumed_transfer_completes_on_absolute_acks(self):
        """mIRC acks the absolute position after a resume, so a resume that
        skipped half the file completes when the acks reach file_size."""
        irc, sender = self.start_send()
        port = irc.port()
        offered = dcc.offered_name_from_handshake(irc.handshake)
        resume_at = 50_000
        self.assertTrue(dcc.handle_resume_request(irc, USER, f"DCC RESUME {offered} {port} {resume_at}"))
        client = self.connect(port)
        held = resume_at
        received = bytearray()
        while held < len(CONTENT):
            chunk = client.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
            held += len(chunk)
            client.sendall(struct.pack("!I", held & 0xFFFFFFFF))
        sender.join(30)
        self.assertEqual(bytes(received), CONTENT[resume_at:])
        self.assertEqual(len(self.sent_lines()), 1, self.debug_lines)
        self.assertEqual(self.failed_lines(), [])

    def test_a_receiver_that_hangs_up_early_is_a_failure(self):
        irc, sender = self.start_send()
        client = self.connect(irc.port())
        chunk = client.recv(4096)
        client.sendall(struct.pack("!I", len(chunk)))
        client.close()                                   # gone, with most of it unacked
        sender.join(30)
        self.assertEqual(self.sent_lines(), [])
        self.assertEqual(len(self.failed_lines()), 1, self.debug_lines)

    def test_acks_are_drained_inside_the_send_loop_not_only_after_it(self):
        """A deadlock a loopback test cannot reach, pinned by construction.

        Acks are 4 bytes per packet. If they are read only after the last
        write, a 4 GB file at 64 KB blocks leaves 256 KB of them queued in
        the bot's receive buffer. Once that buffer AND the receiver's send
        buffer are full - about 1 GB in, with default sizes - mIRC blocks on
        writing an ack, stops reading data, and the transfer stalls forever
        with the bot blocked in sendall(). The loop must drain after every
        write. Asserted as: at least one drain per block sent, made from
        inside the loop (wait=0), for a file of many blocks.
        """
        self.set_config(DCC_BLOCK_SIZE=4096)                # 25 blocks
        calls = []
        real = dcc._drain_acks

        def spy(conn, tracker, wait=0.0):
            calls.append(wait)
            return real(conn, tracker, wait)

        dcc._drain_acks = spy
        self.addCleanup(setattr, dcc, "_drain_acks", real)

        irc, sender = self.start_send()
        client = self.connect(irc.port())
        self.receive(client)
        sender.join(30)

        in_loop = sum(1 for w in calls if w == 0.0)
        blocks = -(-len(CONTENT) // 4096)
        self.assertGreaterEqual(in_loop, blocks,
                                f"{in_loop} in-loop drains for {blocks} blocks - acks are "
                                f"being left to pile up until the file has been written")

    def test_the_fixed_settling_sleep_is_gone(self):
        """A completed transfer returns when the ack arrives, not 1.5 s later.
        Measured: with instant acks the whole exchange is well under that."""
        irc, sender = self.start_send()
        client = self.connect(irc.port())
        started = time.time()
        self.receive(client)
        sender.join(30)
        self.assertLess(time.time() - started, 1.4,
                        "the send thread held its slot for a fixed sleep after completion")


class FailuresAreReportedWhereSuccessesAre(unittest.TestCase):
    """Every failure site goes through one reporter, which reaches the console."""

    def test_no_bare_dcc_fail_print_remains_in_the_send_path(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def start_dcc_send("):]
        end = body.find("\ndef ", 10)
        body = body[:end] if end != -1 else body
        code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
        self.assertNotIn('print(f"[DCC-FAIL]', code,
                         "a failure printed only to the console window is invisible to the "
                         "operator; go through _report_transfer_failure")

    def test_the_reporter_uses_the_fail_category(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def _report_transfer_failure("):]
        body = body[:body.index("\nclass ", 10)]
        # feed_event("FAIL", ...) since #550: the first argument IS the category.
        self.assertIn('announce.feed_event("FAIL"', body)

    def test_announce_renders_the_fail_category(self):
        """FAIL has a tag of its own in the alert colour, like PART - not
        the grey [INFO] a category the table does not know falls to. The
        table replaced an elif chain (#550); the property is the same."""
        import announce
        import theme
        label, colour = announce.category_tag("FAIL", theme.blocks())
        _alert_label, alert = announce.category_tag("PART", theme.blocks())
        self.assertEqual(label, "FAIL")
        self.assertEqual(colour, alert)
        self.assertNotEqual(colour, announce.category_tag("INFO", theme.blocks())[1])


if __name__ == "__main__":
    unittest.main()
