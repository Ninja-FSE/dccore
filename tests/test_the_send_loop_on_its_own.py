"""#1350: start_dcc_send()'s send loop, called directly as stream_the_file().

It was the `with open(...)` block in the middle of start_dcc_send() until
step 4 of splitting it. Here it is handed a file, a connection that records
what it is given, and an _AckTracker; the receiver's acknowledgements come
from a stand-in for _drain_acks(), so no socket is needed and every runner
runs these. test_complete_means_the_receiver_acked_it.py still drives the
loop over loopback, through start_dcc_send().

The one thing the move changed: the `finally` read the loop's bytes_sent,
and a send that raises out of stream_the_file() never returns it. acks.sent
is the same count after every chunk, and the ending reads that.

#1461: the loop reads DCC_BLOCK_SIZE again every pass, so a rehash that
changes it reaches a send already running from its next chunk on, with the
socket timeout set for that chunk before it goes out (TheBlockIsReadEveryPass).

Every nick and file name here is invented.
"""

import ast
import io
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase, parse_source  # noqa: E402

USER = "kilo"
BLOCK = 4096
CONTENT = bytes(range(256)) * 64            # 16,384 bytes: four blocks


class Connection:
    """What the loop sends on: records every sendall(), and the timeout
    that was set when it went out, and fails the chunk numbered `fail_at`
    (1-based) when asked to."""

    def __init__(self, fail_at=None):
        self.chunks = []
        self.timeout = None
        self.timeouts = []          # the timeout in force for each chunk
        self.fail_at = fail_at

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        if self.fail_at is not None and len(self.chunks) + 1 == self.fail_at:
            raise ConnectionResetError("reset by peer")
        self.chunks.append(bytes(data))
        self.timeouts.append(self.timeout)


def acking_everything(conn, tracker, wait=0.0):
    """A receiver that acknowledges all it has been sent, at once."""
    tracker.feed(struct.pack("!I", tracker.sent & 0xFFFFFFFF))


def changing_the_block(*changes):
    """A receiver that acknowledges everything, while DCC_BLOCK_SIZE is set
    as a rehash would set it: (after, value) puts `value` in place once
    `after` chunks have gone out - between two passes of the loop, where a
    reload on another thread lands."""
    pending = dict(changes)

    def drain(conn, tracker, wait=0.0):
        acking_everything(conn, tracker, wait)
        if len(conn.chunks) in pending:
            config.DCC_BLOCK_SIZE = pending.pop(len(conn.chunks))
    return drain


def hanging_up_after(chunks):
    """A receiver that acknowledges the first `chunks` chunks, then closes."""
    def drain(conn, tracker, wait=0.0):
        if len(conn.chunks) <= chunks:
            tracker.feed(struct.pack("!I", tracker.sent & 0xFFFFFFFF))
        else:
            tracker.feed(b"")
    return drain


class LoopHarness(DCCoreTestCase):
    """The file, the rows and stream(); no tests of its own."""

    def setUp(self):
        super().setUp()
        self.set_config(DCC_BLOCK_SIZE=BLOCK)
        tmp = tempfile.mkdtemp(prefix="dccore-loop-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.path = os.path.join(tmp, "Some_Album.zip")
        with io.open(self.path, "wb") as handle:
            handle.write(CONTENT)
        self.row = {"user": USER, "file": "Some_Album.zip", "bytes_sent": 0}
        self.other = {"user": "lima", "file": "other.flac", "bytes_sent": 7}
        self.set_config(active_transfers=[self.other, self.row])
        self.oserve.total_sent_bytes = 0

    def stream(self, conn, drain=acking_everything, resume_offset=0):
        acks = dcc._AckTracker(start=resume_offset)
        with mock.patch.object(dcc, "_drain_acks", drain):
            try:
                sent = dcc.stream_the_file(conn, self.path, resume_offset, resume_offset, acks, self.row, USER)
            except Exception as raised:
                return raised, acks
        return sent, acks


class StreamTheFile(LoopHarness):

    def test_it_sends_the_whole_file_in_blocks_and_returns_the_count(self):
        conn = Connection()
        sent, acks = self.stream(conn)
        self.assertEqual(sent, len(CONTENT))
        self.assertEqual(b"".join(conn.chunks), CONTENT)
        self.assertEqual([len(c) for c in conn.chunks], [BLOCK] * 4)
        self.assertEqual(acks.sent, len(CONTENT))
        self.assertEqual(acks.acked, len(CONTENT))

    def test_the_timeout_covers_one_block(self):
        conn = Connection()
        self.stream(conn)
        self.assertEqual(conn.timeout, dcc._send_timeout(BLOCK))

    def test_each_chunk_moves_this_rows_count_and_the_bots_total_only(self):
        self.stream(Connection())
        self.assertEqual(self.row["bytes_sent"], len(CONTENT))
        self.assertEqual(self.other["bytes_sent"], 7)
        self.assertEqual(self.oserve.total_sent_bytes, len(CONTENT))

    def test_a_resume_sends_from_the_offset_and_counts_the_whole_file(self):
        """The count is what the receiver holds; the row and the bot's total
        move only by what went on the wire."""
        offset = BLOCK + 100
        conn = Connection()
        sent, acks = self.stream(conn, resume_offset=offset)
        self.assertEqual(b"".join(conn.chunks), CONTENT[offset:])
        self.assertEqual(sent, len(CONTENT))
        self.assertEqual(acks.sent, len(CONTENT))
        self.assertEqual(self.row["bytes_sent"], len(CONTENT) - offset)
        self.assertEqual(self.oserve.total_sent_bytes, len(CONTENT) - offset)

    def test_a_receiver_that_closes_mid_file_raises_and_acks_sent_says_how_far(self):
        conn = Connection()
        raised, acks = self.stream(conn, drain=hanging_up_after(1))
        self.assertIsInstance(raised, dcc._ReceiverGone)
        self.assertEqual(raised.acked, BLOCK)
        self.assertEqual(len(conn.chunks), 2)
        self.assertEqual(acks.sent, 2 * BLOCK)
        # The chunk the receiver was gone for is not counted on the row.
        self.assertEqual(self.row["bytes_sent"], BLOCK)

    def test_a_receiver_that_stops_acknowledging_raises_stalled(self):
        conn = Connection()
        acks = dcc._AckTracker()
        with mock.patch.object(dcc, "_drain_acks", lambda *a, **k: None), \
                mock.patch.object(acks, "stalled", return_value=True):
            with self.assertRaises(dcc._ReceiverStalled):
                dcc.stream_the_file(conn, self.path, 0, 0, acks, self.row, USER)
        self.assertEqual(acks.sent, BLOCK)

    def test_a_socket_error_goes_through_with_acks_sent_at_the_last_good_chunk(self):
        conn = Connection(fail_at=3)
        raised, acks = self.stream(conn)
        self.assertIsInstance(raised, ConnectionResetError)
        self.assertEqual(acks.sent, 2 * BLOCK)
        self.assertEqual(self.row["bytes_sent"], 2 * BLOCK)

    def test_the_row_is_found_by_identity_not_by_nick(self):
        """#598: a /nick mid-send rewrites the row's user; the loop must keep
        counting on it."""
        self.row["user"] = "kilo_away"
        self.stream(Connection())
        self.assertEqual(self.row["bytes_sent"], len(CONTENT))


class TheBlockIsReadEveryPass(LoopHarness):
    """#1461: a rehash that changes DCC_BLOCK_SIZE reaches a send already
    running, from its next chunk on. Read once per transfer, a send started
    before a rehash lowered it kept the bigger chunk, and under the upload
    cap took more than an even share of speed_cap's shared clock from the
    sends started after it (test_a_send_that_crawls_while_others_wait.py,
    TheSharedClock). The timeout is set for the new block before the chunk
    it covers goes out.

    The receiver's acks and a resume's offset are byte counts, so a block
    that changes mid-file changes nothing for the receiver: the bytes and
    every count stay exact."""

    def write(self, content):
        with io.open(self.path, "wb") as handle:
            handle.write(content)
        return content

    def sizes(self, conn):
        return [len(chunk) for chunk in conn.chunks]

    def test_a_lowered_block_takes_effect_from_the_next_chunk(self):
        """16 KB, 8 KB a pass, lowered to 4 KB after the first chunk: 8, 4, 4.
        Read once per transfer it was 8, 8."""
        self.set_config(DCC_BLOCK_SIZE=8192)
        conn = Connection()
        sent, acks = self.stream(conn, drain=changing_the_block((1, 4096)))
        self.assertEqual(self.sizes(conn), [8192, 4096, 4096])
        self.assertEqual(b"".join(conn.chunks), CONTENT)
        self.assertEqual(sent, len(CONTENT))
        self.assertEqual((acks.sent, acks.acked), (len(CONTENT), len(CONTENT)))
        self.assertEqual(conn.timeout, dcc._send_timeout(4096))
        self.assertEqual(self.row["bytes_sent"], len(CONTENT))
        self.assertEqual(self.oserve.total_sent_bytes, len(CONTENT))

    def test_a_raised_block_takes_effect_from_the_next_chunk(self):
        """4 KB a pass, raised to 8 KB after the first chunk: 4, 8, 4 (the
        rest of the file). Read once per transfer it was 4, 4, 4, 4."""
        conn = Connection()
        sent, acks = self.stream(conn, drain=changing_the_block((1, 8192)))
        self.assertEqual(self.sizes(conn), [4096, 8192, 4096])
        self.assertEqual(b"".join(conn.chunks), CONTENT)
        self.assertEqual(sent, len(CONTENT))
        self.assertEqual((acks.sent, acks.acked), (len(CONTENT), len(CONTENT)))
        self.assertEqual(conn.timeout, dcc._send_timeout(8192))

    def test_above_64_kb_the_new_timeout_is_set_before_the_bigger_chunk_goes_out(self):
        """128 KB raised to 256 KB: the timeout grows with the block above
        64 KB (#1139), 120 s to 240 s, and the 256 KB sendall() must already
        have its 240 s - with the old 120 s the slowest receiver a send
        survives would rise from 64 KB a minute to 128 KB."""
        self.assertEqual((dcc._send_timeout(131072), dcc._send_timeout(262144)), (120.0, 240.0))
        content = self.write(os.urandom(131072 + 2 * 262144))
        self.set_config(DCC_BLOCK_SIZE=131072)
        conn = Connection()
        sent, _acks = self.stream(conn, drain=changing_the_block((1, 262144)))
        self.assertEqual(self.sizes(conn), [131072, 262144, 262144])
        self.assertEqual(conn.timeouts, [120.0, 240.0, 240.0])
        self.assertEqual(b"".join(conn.chunks), content)
        self.assertEqual(sent, len(content))

    def test_lowered_from_above_64_kb_the_timeout_comes_down_with_it(self):
        content = self.write(os.urandom(262144 + 2 * 131072))
        self.set_config(DCC_BLOCK_SIZE=262144)
        conn = Connection()
        self.stream(conn, drain=changing_the_block((1, 131072)))
        self.assertEqual(self.sizes(conn), [262144, 131072, 131072])
        self.assertEqual(conn.timeouts, [240.0, 120.0, 120.0])
        self.assertEqual(b"".join(conn.chunks), content)

    def test_a_resumed_send_keeps_every_count_exact_across_a_change(self):
        """From byte 100, 8 KB lowered to 4 KB: 8192, 4096, then the 3,996
        left. What the loop returns and what the tracker holds is the whole
        file; the row and the bot's total moved by what went on the wire."""
        offset = 100
        self.set_config(DCC_BLOCK_SIZE=8192)
        conn = Connection()
        sent, acks = self.stream(conn, drain=changing_the_block((1, 4096)), resume_offset=offset)
        self.assertEqual(self.sizes(conn), [8192, 4096, len(CONTENT) - offset - 8192 - 4096])
        self.assertEqual(b"".join(conn.chunks), CONTENT[offset:])
        self.assertEqual(sent, len(CONTENT))
        self.assertEqual((acks.sent, acks.acked), (len(CONTENT), len(CONTENT)))
        self.assertEqual(self.row["bytes_sent"], len(CONTENT) - offset)
        self.assertEqual(self.oserve.total_sent_bytes, len(CONTENT) - offset)

    def test_a_new_value_is_clamped_like_the_first(self):
        """A mistyped value on a rehash is clamped as at the start of a send
        (dcc_block_size()): 100 becomes MIN_DCC_BLOCK_SIZE, half a gigabyte
        MAX_DCC_BLOCK_SIZE, with its timeout."""
        low, high = dcc.MIN_DCC_BLOCK_SIZE, dcc.MAX_DCC_BLOCK_SIZE
        with self.subTest(to=100):
            self.set_config(DCC_BLOCK_SIZE=8192)
            conn = Connection()
            self.stream(conn, drain=changing_the_block((1, 100)))
            self.assertEqual(self.sizes(conn), [8192, low, low])
            self.assertEqual(b"".join(conn.chunks), CONTENT)
            self.assertEqual(conn.timeout, dcc._send_timeout(low))
        with self.subTest(to=500000000):
            content = self.write(os.urandom(524288 + 2 * high))
            self.set_config(DCC_BLOCK_SIZE=524288)
            conn = Connection()
            self.stream(conn, drain=changing_the_block((1, 500000000)))
            self.assertEqual(self.sizes(conn), [524288, high, high])
            self.assertEqual(conn.timeouts, [dcc._send_timeout(524288), dcc._send_timeout(high),
                                             dcc._send_timeout(high)])
            self.assertEqual(conn.timeout, 960.0)
            self.assertEqual(b"".join(conn.chunks), content)

    def test_a_rehash_window_moves_one_pass_at_the_default_and_the_next_goes_back(self):
        """A !rehash re-executes defaults.py, and for its few milliseconds
        DCC_BLOCK_SIZE is the shipped 64 KB (runtime.config_reload_lock). A
        pass that reads it then moves 64 KB under a 60 s timeout - both
        consistent with each other - and the next pass is back on the
        operator's 128 KB and its 120 s."""
        self.assertEqual(dcc.DEFAULT_DCC_BLOCK_SIZE, 65536)
        content = self.write(os.urandom(2 * 131072 + 65536 + 131072))
        self.set_config(DCC_BLOCK_SIZE=131072)
        conn = Connection()
        sent, acks = self.stream(conn, drain=changing_the_block((1, dcc.DEFAULT_DCC_BLOCK_SIZE),
                                                                (2, 131072)))
        self.assertEqual(self.sizes(conn), [131072, 65536, 131072, 131072])
        self.assertEqual(conn.timeouts, [120.0, 60.0, 120.0, 120.0])
        self.assertEqual(b"".join(conn.chunks), content)
        self.assertEqual((sent, acks.acked), (len(content), len(content)))

    def test_an_unchanged_block_sets_the_timeout_once(self):
        """The timeout is set again only when the block changed: a send whose
        block never changes sets it once, before its first chunk."""
        calls = []

        class Counting(Connection):
            def settimeout(self, value):
                calls.append(value)
                super().settimeout(value)

        conn = Counting()
        self.stream(conn)
        self.assertEqual(self.sizes(conn), [BLOCK] * 4)
        self.assertEqual(calls, [dcc._send_timeout(BLOCK)])


class TheEndingReadsTheTracker(unittest.TestCase):
    """start_dcc_send() hands finish_send() acks.sent once the tracker exists
    - the loop's count when it raised - and its own bytes_sent before that.
    Read from the source here, so it holds on a runner without loopback;
    test_complete_means_the_receiver_acked_it.py drives it."""

    def test_the_sent_file_takes_acks_sent_when_there_is_a_tracker(self):
        with io.open(os.path.join(REPO_ROOT, "src", "dcc.py"), encoding="utf-8") as handle:
            tree = parse_source(handle.read())
        start = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "start_dcc_send")
        main_try = next(node for node in start.body if isinstance(node, ast.Try) and node.finalbody)
        calls = [node for node in ast.walk(ast.Module(body=main_try.finalbody, type_ignores=[]))
                 if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "SentFile"]
        self.assertEqual(len(calls), 1)
        fields = dcc.SentFile._fields
        bytes_sent = calls[0].args[fields.index("bytes_sent")]
        self.assertEqual(ast.unparse(bytes_sent), "_here['acks'].sent if 'acks' in _here else bytes_sent")

    def test_the_loop_is_called_with_the_sends_row_and_its_count(self):
        with io.open(os.path.join(REPO_ROOT, "src", "dcc.py"), encoding="utf-8") as handle:
            tree = parse_source(handle.read())
        start = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "start_dcc_send")
        lines = [ast.unparse(node) for node in ast.walk(start)
                 if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                 and getattr(node.value.func, "id", None) == "stream_the_file"]
        self.assertEqual(lines, ["bytes_sent = stream_the_file(conn, file_path, resume_offset, bytes_sent, "
                                 "acks, _row, user)"])


if __name__ == "__main__":
    unittest.main()
