"""A raise inside the STATUS burst ended all output while the session
stayed open (audit L17, #681).

_writer_loop() called send_status() -> status_lines() at two points with
no guard but the one around the stats block inside status_lines(). A
dcc_queue value with no len(), or an active_transfers row whose
started_at was a string, raised out of the writer thread: the session was
a black hole - the reader still accepted commands, every later send() was
queued and never written, and after 90 s dccore.mrc called the link dead
and reconnected, into the same wall. Reachable today only through a
hand-edited or future mis-typed row; every in-tree producer is well-typed.

Since #614 the figures are computed on a helper thread whose body catches
and prints ("Status burst failed: ..."), so the writer already survives -
this pins it with the audit's own scenarios, so a later change to where
the burst is computed cannot quietly reopen it.
"""

import contextlib
import io
import os
import socket
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def recv_until(sock, needle, seconds=3.0):
    sock.settimeout(seconds)
    buffer = b""
    deadline = time.time() + seconds
    while time.time() < deadline and needle not in buffer:
        try:
            buffer += sock.recv(65536)
        except socket.timeout:
            break
    return buffer


class TheWriterOutlivesABadRow(DCCoreTestCase):

    def running_session(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        s = adminchat.Session(a, "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = True
        s.structured = True
        return s, b

    def burst_then_a_line(self, s, far):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s.start_writer()
            s.request_status()
            s.send("DCCORE OUT still here")
            buffer = recv_until(far, b"still here\n")
        return buffer, out.getvalue()

    def test_a_queue_value_with_no_len_does_not_end_the_session(self):
        """The verifier's case: an int where a list of rows should be."""
        config.dcc_queue["x"] = 5
        s, far = self.running_session()

        buffer, printed = self.burst_then_a_line(s, far)

        self.assertIn(b"DCCORE OUT still here\n", buffer, "the writer died with the burst")
        self.assertIn("Status burst failed", printed)
        self.assertFalse(s.closed)

    def test_a_transfer_row_with_a_string_start_time_does_not_either(self):
        config.active_transfers.append({"user": "dana", "file": "x", "bytes_sent": 0, "started_at": "abc"})
        s, far = self.running_session()

        buffer, printed = self.burst_then_a_line(s, far)

        self.assertIn(b"DCCORE OUT still here\n", buffer)
        self.assertIn("Status burst failed", printed)

    def test_the_burst_is_back_once_the_row_is_gone(self):
        """The failure is per burst, not per session."""
        config.dcc_queue["x"] = 5
        s, far = self.running_session()
        self.burst_then_a_line(s, far)
        config.dcc_queue.pop("x", None)

        with contextlib.redirect_stdout(io.StringIO()):
            s.request_status()
            buffer = recv_until(far, b"DCCORE STATUS ")

        self.assertIn(b"DCCORE STATUS ", buffer, "no burst after the bad row was removed")


if __name__ == "__main__":
    unittest.main()
