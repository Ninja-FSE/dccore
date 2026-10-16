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
    """What the loop sends on: records every sendall(), and fails the
    chunk numbered `fail_at` (1-based) when asked to."""

    def __init__(self, fail_at=None):
        self.chunks = []
        self.timeout = None
        self.fail_at = fail_at

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        if self.fail_at is not None and len(self.chunks) + 1 == self.fail_at:
            raise ConnectionResetError("reset by peer")
        self.chunks.append(bytes(data))


def acking_everything(conn, tracker, wait=0.0):
    """A receiver that acknowledges all it has been sent, at once."""
    tracker.feed(struct.pack("!I", tracker.sent & 0xFFFFFFFF))


def hanging_up_after(chunks):
    """A receiver that acknowledges the first `chunks` chunks, then closes."""
    def drain(conn, tracker, wait=0.0):
        if len(conn.chunks) <= chunks:
            tracker.feed(struct.pack("!I", tracker.sent & 0xFFFFFFFF))
        else:
            tracker.feed(b"")
    return drain


class StreamTheFile(DCCoreTestCase):

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
