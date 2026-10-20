"""#1352: MAX_UPLOAD_KB and MAX_DOWNLOAD_KB, the most the bot's transfers may
use of the line, per direction.

speed_cap.py is driven here on a clock the test owns, so the pace is checked
to the byte without waiting for it. Then the two loops that use it: a fetch
over a socket pair (every runner), and a send over loopback with a real cap
and a real clock, paired with a check of the send loop's source for a runner
that cannot dial loopback.

Every nick and file name here is invented.
"""

import ast
import io
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import speed_cap  # noqa: E402

from tests import dcc_ports  # noqa: E402
from tests.support import DCCoreTestCase, parse_source, silence_debug  # noqa: E402
from tests.test_a_failing_bot_is_paused import TcpLike  # noqa: E402
from tests.test_dcc_resume_end_to_end import RecordingIrcSocket, loopback_is_usable as probe_loopback  # noqa: E402

UP, DOWN = speed_cap.UPLOAD, speed_cap.DOWNLOAD
# The window ASendUnderACap sends on; a parallel run moves it (#1146).
PORT_START, PORT_END = dcc_ports(51340, 51350)


def loopback_is_usable():
    """The shared probe, on the window this module's real sends use (#1469):
    test_dcc_resume_end_to_end's own window can be free while this one is not."""
    return probe_loopback(PORT_START, PORT_END)


class Clock:
    """A clock that moves only when something sleeps on it."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class CapCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(MAX_UPLOAD_KB=0, MAX_DOWNLOAD_KB=0)
        self.clock = Clock()

    def take(self, direction, nbytes):
        return speed_cap.take(direction, nbytes, clock=self.clock, sleep=self.clock.sleep)


class NoCapIsNoLimit(CapCase):

    def test_zero_never_waits_and_keeps_the_whole_block(self):
        for _ in range(50):
            self.assertEqual(self.take(UP, 65536), 0.0)
        self.assertEqual(self.clock.slept, [])
        self.assertEqual(speed_cap.chunk_size(UP, 65536), 65536)

    def test_a_value_that_is_not_a_positive_number_is_no_limit(self):
        for value in (-5, "fast", None, ""):
            with self.subTest(value=value):
                self.set_config(MAX_UPLOAD_KB=value)
                self.assertEqual(speed_cap.rate(UP), 0.0)
                self.assertEqual(self.take(UP, 65536), 0.0)

    def test_both_are_off_out_of_the_box(self):
        with io.open(os.path.join(REPO_ROOT, "src", "defaults.py"), encoding="utf-8") as handle:
            tree = parse_source(handle.read())
        shipped = {node.target.id: ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                   and node.target.id in (UP, DOWN)}
        self.assertEqual(shipped, {UP: 0, DOWN: 0})


class ThePace(CapCase):

    def test_the_cap_is_kept_to_the_byte(self):
        """100 KB/s, forty 10 KB chunks: the first goes at once, and the
        fortieth starts 39 chunks' worth of line later."""
        self.set_config(MAX_UPLOAD_KB=100)
        start = self.clock.now
        for _ in range(40):
            self.take(UP, 10240)
        self.assertAlmostEqual(self.clock.now - start, 39 * 0.1, places=9)

    def test_the_first_chunk_after_a_quiet_spell_goes_at_once_and_the_second_waits(self):
        """An idle line saves up nothing: no burst after a pause."""
        self.set_config(MAX_UPLOAD_KB=100)
        self.take(UP, 10240)
        self.clock.now += 3600
        self.assertEqual(self.take(UP, 10240), 0.0)
        self.assertAlmostEqual(self.take(UP, 10240), 0.1, places=9)

    def test_transfers_share_one_cap(self):
        """Two sends taking turns move the cap between them, not twice it."""
        self.set_config(MAX_UPLOAD_KB=100)
        start = self.clock.now
        for _ in range(20):
            self.take(UP, 10240)        # one send
            self.take(UP, 10240)        # another
        self.assertAlmostEqual(self.clock.now - start, 39 * 0.1, places=9)

    def test_upload_and_download_are_separate(self):
        self.set_config(MAX_UPLOAD_KB=10, MAX_DOWNLOAD_KB=10)
        self.take(UP, 102400)           # ten seconds of the upload line
        self.assertEqual(self.take(DOWN, 10240), 0.0)
        self.assertAlmostEqual(self.take(UP, 1), 10.0, places=9)

    def test_a_new_cap_starts_a_new_clock(self):
        """Reservations made at 1 KB/s do not hold up a line raised to 1 MB/s."""
        self.set_config(MAX_UPLOAD_KB=1)
        for _ in range(5):
            self.take(UP, 4096)         # four seconds of line each
        self.set_config(MAX_UPLOAD_KB=1024)
        before = self.clock.now
        self.assertEqual(self.take(UP, 4096), 0.0)
        self.assertEqual(self.clock.now, before)

    def test_the_clock_is_runtimes(self):
        """Kept where a !rehash cannot replace it under running transfers."""
        self.set_config(MAX_UPLOAD_KB=100)
        self.take(UP, 10240)
        self.assertIn(UP, runtime.speed_cap_clocks)
        self.assertIs(config.speed_cap_clocks, runtime.speed_cap_clocks)


class TheChunk(CapCase):

    def test_under_a_cap_a_chunk_is_a_tenth_of_a_second_of_line(self):
        self.set_config(MAX_UPLOAD_KB=200)
        self.assertEqual(speed_cap.chunk_size(UP, 65536), 20480)

    def test_never_below_four_kb(self):
        self.set_config(MAX_UPLOAD_KB=1)
        self.assertEqual(speed_cap.chunk_size(UP, 65536), 4096)

    def test_never_above_the_transfers_own_block(self):
        self.set_config(MAX_UPLOAD_KB=100000)
        self.assertEqual(speed_cap.chunk_size(UP, 65536), 65536)

    def test_a_change_is_read_on_the_next_chunk(self):
        self.set_config(MAX_DOWNLOAD_KB=200)
        self.assertEqual(speed_cap.chunk_size(DOWN, 65536), 20480)
        self.set_config(MAX_DOWNLOAD_KB=400)
        self.assertEqual(speed_cap.chunk_size(DOWN, 65536), 40960)


class TheStallClockDoesNotCountOurOwnWaiting(unittest.TestCase):
    """A receiver that holds everything it was sent owes nothing, so a pause
    on our side - the upload cap's - is not its silence."""

    def test_a_receiver_that_acked_everything_is_not_stalled_by_our_pause(self):
        t = dcc._AckTracker()
        t.sent = 4096
        t.feed(struct.pack("!I", 4096))
        later = t.last_advance_at + dcc.ACK_STALL_SECONDS + 30
        t.sending(now=later)
        t.sent = 8192                   # the next chunk goes
        self.assertFalse(t.stalled(now=later + 1))

    def test_a_receiver_that_owes_bytes_keeps_its_clock(self):
        t = dcc._AckTracker()
        t.sent = 8192
        t.feed(struct.pack("!I", 4096))
        advanced = t.last_advance_at
        t.sending(now=advanced + dcc.ACK_STALL_SECONDS + 30)
        self.assertEqual(t.last_advance_at, advanced)
        self.assertTrue(t.stalled(now=advanced + dcc.ACK_STALL_SECONDS + 31))


def _send_loop():
    """The send loop: the `while` that sends each chunk, wherever in dcc.py
    it lives now."""
    with io.open(os.path.join(REPO_ROOT, "src", "dcc.py"), encoding="utf-8") as handle:
        tree = parse_source(handle.read())
    loops = [node for node in ast.walk(tree) if isinstance(node, ast.While)
             and any(isinstance(n, ast.Call) and ast.unparse(n) == "conn.sendall(chunk)"
                     for n in ast.walk(node))]
    assert len(loops) == 1, len(loops)
    return loops[0]


class TheSendLoopUsesTheUploadCap(unittest.TestCase):
    """Read from the source, so it holds on a runner without loopback;
    ASendUnderACap below drives it."""

    def test_each_chunk_is_sized_and_paced_by_the_cap_before_it_goes(self):
        body = _send_loop().body
        steps = [ast.unparse(node) for node in body]
        sent_at = next(i for i, step in enumerate(steps) if "conn.sendall(chunk)" in step)
        # The read sits in a try of its own since #1389, which turns the
        # file's OSError into _SourceUnreadable; the read itself is the same.
        first = body[0].body[0] if isinstance(body[0], ast.Try) else body[0]
        self.assertEqual(ast.unparse(first), "chunk = f.read(speed_cap.chunk_size(speed_cap.UPLOAD, block))")
        before = steps[:sent_at]
        self.assertIn("speed_cap.take(speed_cap.UPLOAD, len(chunk))", before)
        self.assertIn("acks.sending()", before)
        self.assertLess(before.index("speed_cap.take(speed_cap.UPLOAD, len(chunk))"),
                        before.index("acks.sending()"),
                        "the stall clock restarts after our wait, not before it")


class AFetchUnderACap(DCCoreTestCase):
    """The receive loop, over a socket pair."""

    PAYLOAD = bytes(range(256)) * 64            # 16,384 bytes

    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#chan", FETCH_MAX_PER_BOT=0, MAX_DOWNLOAD_KB=4,
                        FETCH_TRANSFER_TIMEOUT=600)
        config.channel_users["#chan"] = {"serverone"}
        silence_debug(announce)
        # dcc_fetch's wall clock, moved by the cap's waiting alone: each chunk
        # "waits" 1000 s, so four chunks outlast FETCH_TRANSFER_TIMEOUT.
        self.now = [5000.0]
        self.taken = []

        def take(direction, nbytes):
            self.taken.append((direction, nbytes))
            self.now[0] += 1000.0
            return 1000.0
        fake_time = types.SimpleNamespace(time=lambda: self.now[0])
        fake_time.__dict__.update({k: getattr(time, k) for k in ("sleep", "monotonic", "perf_counter",
                                                                  "strftime", "localtime", "gmtime")})
        patcher = mock.patch.object(dcc_fetch, "time", fake_time)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(speed_cap, "take", take)
        patcher.start()
        self.addCleanup(patcher.stop)

    def receive(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)

        # The far end writes from its own thread: a macOS socket pair holds
        # about 8 KB, so writing all 16 KB before the loop reads any of it
        # never returned there.
        def offer():
            theirs.sendall(self.PAYLOAD)
            theirs.shutdown(socket.SHUT_WR)
        writer = threading.Thread(target=offer, daemon=True)
        writer.start()
        self.addCleanup(writer.join, 10)
        rid = dcc_fetch.enqueue_fetch("ServerOne", "Some_Track.flac", request_type="file")
        row = config.fetch_queue[rid]
        row.update(state="receiving", request_type="file")
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        dcc_fetch._run_transfer(row, {"size": len(self.PAYLOAD), "ip": None, "port": 0}, dest,
                                "Some_Track.flac", sock=TcpLike(ours))
        return row

    def test_every_chunk_is_paced_by_the_download_cap(self):
        self.receive()
        self.assertEqual(sum(n for _, n in self.taken), len(self.PAYLOAD))
        self.assertEqual({d for d, _ in self.taken}, {DOWN})
        self.assertTrue(all(n <= 4096 for _, n in self.taken), self.taken)

    def test_our_own_waiting_does_not_time_the_fetch_out(self):
        """Four 1000 s waits against a 600 s ceiling: still complete."""
        row = self.receive()
        self.assertGreaterEqual(len(self.taken), 4)
        self.assertEqual(row["state"], "complete", row.get("error"))


@unittest.skipUnless(loopback_is_usable(), "this runner cannot bind a listener and dial loopback")
class ASendUnderACap(DCCoreTestCase):
    """A real send over loopback, with a real cap on a real clock."""

    CONTENT = bytes(range(256)) * 400          # 102,400 bytes

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-cap-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.served = os.path.join(self.tmp, "Some_Album.zip")
        with io.open(self.served, "wb") as handle:
            handle.write(self.CONTENT)
        self.set_config(
            active_transfers=[{"user": "kilo", "file": "Some_Album.zip", "bytes_sent": 0,
                               "next_file_obj": "Some_Album.zip"}],
            MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8", DCC_PORT_START=PORT_START,
            DCC_PORT_END=PORT_END, MAX_UPLOAD_KB=200)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        self.debug_lines = []
        real = announce.send_debug
        announce.send_debug = lambda text, category="INFO": self.debug_lines.append((category, text))
        self.addCleanup(setattr, announce, "send_debug", real)
        self.oserve.total_sent_bytes = 0

    def send_and_receive(self):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, "kilo", self.served, "Some_Album.zip", "#somechannel", "Some_Album.zip"),
            daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc.handshake_seen.wait(20), "no DCC SEND handshake")
        client = socket.create_connection(("127.0.0.1", irc.port()), timeout=20)
        client.settimeout(20)
        self.addCleanup(client.close)
        held = bytearray()
        started = time.monotonic()
        while len(held) < len(self.CONTENT):
            chunk = client.recv(65536)
            if not chunk:
                break
            held.extend(chunk)
            client.sendall(struct.pack("!I", len(held) & 0xFFFFFFFF))
        elapsed = time.monotonic() - started
        sender.join(30)
        return bytes(held), elapsed

    def test_the_send_takes_as_long_as_the_cap_says_and_arrives_whole(self):
        """100 KB at 200 KB/s in 20 KB chunks: the first goes at once, the
        other four wait a tenth of a second each. A lower bound only - a
        slow runner can take longer, never less."""
        got, elapsed = self.send_and_receive()
        self.assertEqual(got, self.CONTENT)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertEqual([t for c, t in self.debug_lines if c == "FAIL"], [])

    def test_every_chunk_is_reserved_from_the_upload_clock(self):
        taken = []
        real = speed_cap.take

        def recording(direction, nbytes, **kwargs):
            taken.append((direction, nbytes))
            return real(direction, nbytes, **kwargs)
        with mock.patch.object(speed_cap, "take", recording):
            got, _ = self.send_and_receive()
        self.assertEqual(got, self.CONTENT)
        self.assertEqual(taken, [(UP, 20480)] * 5)


if __name__ == "__main__":
    unittest.main()
