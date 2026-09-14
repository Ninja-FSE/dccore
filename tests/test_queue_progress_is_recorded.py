"""The Queue table's progress bar needs a total to measure bytes_sent
against.

dcc.py already kept config.active_transfers[n]["bytes_sent"] live, updated
per chunk as a send runs (see start_dcc_send()'s own send loop) - but
nothing ever recorded what it was a fraction OF. webserver.build_queue_payload()
can show "N bytes moved" but never "N% done" without the file's total size
sitting somewhere reachable.

Real end-to-end, the same way tests/test_dcc_resume_end_to_end.py proves the
resume byte offset: a real listening socket, dcc.start_dcc_send() running on
its own thread, and a real client dialling in and reading partway through -
proving the size lands on the row before any byte is sent, and bytes_sent
keeps moving against it while the transfer is still in flight.

WHY THIS IS ALLOWED TO SKIP. Same reason as test_dcc_resume_end_to_end.py: it
binds a listening socket and dials loopback, which a sandboxed runner may
permit neither. The probe is a real bind-and-connect on the same range this
file then uses, so a skip means the environment cannot host the test, not
that something merely looked wrong.
"""

import io
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

USER = "someuser"
PORT_START = 51300
PORT_END = 51310
CONTENT = bytes(range(256)) * 100000  # 25,600,000 bytes (~24MB)


def loopback_is_usable():
    for port in range(PORT_START, PORT_END + 1):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("0.0.0.0", port))
            listener.listen(1)
            client = socket.create_connection(("127.0.0.1", port), timeout=2)
            conn, _ = listener.accept()
            conn.close()
            client.close()
            return True
        except OSError:
            continue
        finally:
            try:
                listener.close()
            except OSError:
                pass
    return False


class RecordingIrcSocket:
    def __init__(self):
        self.lines = []
        self.handshake = None
        self.handshake_seen = threading.Event()

    def sendall(self, payload):
        # Delegated (#504), same reason as the end-to-end resume fixture.
        self.send(payload)

    def send(self, payload):
        text = payload.decode("utf-8", "replace")
        self.lines.append(text)
        if "DCC SEND " in text and self.handshake is None:
            self.handshake = text
            self.handshake_seen.set()
        return len(payload)

    def port(self):
        fields = self.handshake.split("DCC SEND ", 1)[1].rsplit(" ", 3)
        return int(fields[2])


@unittest.skipUnless(loopback_is_usable(),
                     "this runner cannot bind a listener and dial loopback")
class TheTransferRowCarriesItsOwnSize(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-progress-e2e-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.served = os.path.join(self.tmp, "Big_File.bin")
        with io.open(self.served, "wb") as handle:
            handle.write(CONTENT)

        self.set_config(
            active_transfers=[{"user": USER, "file": "Big_File.bin", "bytes_sent": 0,
                               "next_file_obj": "Big_File.bin"}],
            MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8",
            DCC_PORT_START=PORT_START, DCC_PORT_END=PORT_END)

        self.irc = RecordingIrcSocket()
        # #430: start_dcc_send() dispatches through the LIVE socket
        # sys.modules['oserve'] reports, not the thread argument alone.
        self.oserve.irc_connection = self.irc
        self.sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(self.irc, USER, self.served, "Big_File.bin", "#somechannel",
                  "Big_File.bin"),
            daemon=True)
        self.sender.start()
        self.addCleanup(self.sender.join, 30)
        self.client = None
        self.addCleanup(self._close_client)

    def _close_client(self):
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass

    def dial(self):
        self.assertTrue(self.irc.handshake_seen.wait(20),
                        "no DCC SEND handshake was ever sent")
        self.client = socket.create_connection(("127.0.0.1", self.irc.port()), timeout=20)
        self.client.settimeout(20)
        return self.client

    def drain(self):
        """Read to EOF so the sender thread's loop ends and it cleans up."""
        while True:
            try:
                chunk = self.client.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break

    def test_the_size_is_recorded_before_any_byte_is_read(self):
        self.dial()

        # The handshake carries the size too, but the row is what the
        # dashboard actually reads - this is asserting the ROW, not the wire.
        import defaults as config
        self.assertTrue(config.active_transfers,
                        "the transfer row disappeared before anything was read")
        self.assertEqual(config.active_transfers[0]["size"], len(CONTENT))

        self.drain()

    def _read_watching_for_mid_flight(self, client, sample):
        """Drain in small steps, calling `sample()` after every one, until
        either it reports success or the file finishes.

        Not a single read-then-check: bytes_sent is only updated in whole
        DCC_BLOCK_SIZE (64KB) jumps, right after the sender's own sendall()
        returns for that chunk - and the interpreter can switch threads on
        any bytecode boundary, not only at I/O calls, so a single check
        immediately after one 64KB read can land in the narrow window before
        that update statement has run. A first attempt at this used a 1MB
        file, which passed reliably on Linux CI but failed EVERY time on
        Windows: a coarser thread-scheduling quantum there let the sender
        run all ~16 chunks of a 1MB file to completion - reaching size and
        even finishing the transfer - inside one scheduler slice, before
        this thread's poll loop ever ran a single sample, no matter how
        small the read step was. CONTENT is now ~24MB, large enough that
        completing the whole transfer within one scheduler quantum on any
        platform this suite's CI covers is implausible, so reading in small
        4KB steps and sampling after each one reliably lands inside the
        transfer rather than after it.
        """
        received = 0
        deadline = time.time() + 20
        while received < len(CONTENT) and time.time() < deadline:
            chunk = client.recv(4096)
            if not chunk:
                break
            received += len(chunk)
            if sample():
                return True
        return False

    def test_bytes_sent_advances_against_that_size_while_still_in_flight(self):
        import defaults as config
        client = self.dial()

        def sample():
            row = config.active_transfers[0] if config.active_transfers else None
            return row is not None and 0 < row.get("bytes_sent", 0) < row.get("size", 0)

        seen_mid_flight = self._read_watching_for_mid_flight(client, sample)

        self.assertTrue(seen_mid_flight,
                        "never observed 0 < bytes_sent < size while the "
                        "transfer was still running")
        self.drain()

    def test_the_webserver_payload_reports_a_genuine_in_progress_percentage(self):
        """The point of recording it at all: build_queue_payload() can now
        answer with something other than 0% or 100%."""
        client = self.dial()
        seen_pct = {}

        def sample():
            rows = {row["user"]: row for row in webserver.build_queue_payload()}
            row = rows.get(USER)
            if row is None or not row.get("size"):
                return False
            pct = 100 * row["bytes_sent"] / row["size"]
            if 0 < pct < 100:
                seen_pct["value"] = pct
                return True
            return False

        found = self._read_watching_for_mid_flight(client, sample)

        self.assertTrue(found, "build_queue_payload() never reported a "
                               "percentage strictly between 0 and 100 while "
                               "the transfer was still running")
        self.assertGreater(seen_pct["value"], 0)
        self.assertLess(seen_pct["value"], 100)

        self.drain()


if __name__ == "__main__":
    unittest.main()
