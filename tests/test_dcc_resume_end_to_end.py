"""A resumed send puts the tail of the file on the wire, and nothing else.

tests/test_dcc_resume.py covers the protocol and the wiring. This runs the
whole thing: a real listening socket, a real client connecting to it, and a
byte-for-byte check of what arrived.

It is the test that would have caught an off-by-one in the seek, a seek
applied to the wrong handle, or an ACCEPT whose position and whose seek
disagreed - none of which a source-reading guard can see.

WHY THIS IS ALLOWED TO SKIP. It binds a listening socket and dials the
loopback address. A sandboxed or locked-down runner may permit neither, and
this suite's rule for an environment-dependent hazard is to PROBE it and skip
rather than assert it universally. The probe is a real bind and a real
connect on the same port range the test then uses, so a skip means the
environment genuinely cannot host the test rather than that some proxy for it
looked wrong.
"""

import io
import os
import socket
import sys
import tempfile
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

USER = "someuser"
PORT_START = 51200
PORT_END = 51210
# Distinctive rather than random: a wrong offset shows up as a readable
# mismatch in the failure message instead of a wall of identical bytes.
CONTENT = bytes(range(256)) * 400          # 102,400 bytes
RESUME_AT = 40_000


def loopback_is_usable():
    """Bind and dial for real, on the range the test uses."""
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
    """Stands in for the IRC connection. The handshake is what tells the test
    which port to dial, so it also signals when it has seen one."""

    def __init__(self):
        self.lines = []
        self.handshake = None
        self.handshake_seen = threading.Event()

    def sendall(self, payload):
        # Delegated (#504): the handshake this test waits on goes out through
        # sendall() now, and a fake that answered only send() would sit on
        # its Event until the timeout.
        self.send(payload)

    def send(self, payload):
        text = payload.decode("utf-8", "replace")
        self.lines.append(text)
        if "DCC SEND " in text and self.handshake is None:
            self.handshake = text
            self.handshake_seen.set()
        return len(payload)

    def port(self):
        """The port out of "DCC SEND <name> <ip> <port> <size>"."""
        fields = self.handshake.split("DCC SEND ", 1)[1].rsplit(" ", 3)
        return int(fields[2])

    def accept_positions(self):
        return [line for line in self.lines if "DCC ACCEPT " in line]


@unittest.skipUnless(loopback_is_usable(),
                     "this runner cannot bind a listener and dial loopback")
class TheWholeExchange(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-resume-e2e-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        self.served = os.path.join(self.tmp, "Some_Album.zip")
        with io.open(self.served, "wb") as handle:
            handle.write(CONTENT)

        self.set_config(
            active_transfers=[{"user": USER, "file": "Some_Album.zip",
                               "bytes_sent": 0,
                               "next_file_obj": "Some_Album.zip"}],
            MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8",
            DCC_PORT_START=PORT_START, DCC_PORT_END=PORT_END)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)

    def run_transfer(self, resume_at=None):
        """Offer the file, optionally resume it, and return what arrived.

        The send blocks in accept(), so it runs on its own thread and the
        test plays the receiver - which is the way round the real system
        works too.
        """
        irc = RecordingIrcSocket()
        # #430: start_dcc_send() dispatches through the LIVE socket
        # sys.modules['oserve'] reports, not the thread argument alone.
        self.oserve.irc_connection = irc
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, USER, self.served, "Some_Album.zip", "#somechannel",
                  "Some_Album.zip"),
            daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)

        self.assertTrue(irc.handshake_seen.wait(20),
                        "no DCC SEND handshake was ever sent")
        port = irc.port()

        if resume_at is not None:
            offered = dcc.offered_name_from_handshake(irc.handshake)
            self.assertTrue(
                dcc.handle_resume_request(
                    irc, USER, f"DCC RESUME {offered} {port} {resume_at}"),
                "the resume request was not accepted")

        received = bytearray()
        client = socket.create_connection(("127.0.0.1", port), timeout=20)
        try:
            client.settimeout(20)
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                received.extend(chunk)
        finally:
            client.close()
        sender.join(30)
        return irc, bytes(received)

    def test_a_resumed_send_starts_at_the_agreed_byte(self):
        """The point of the whole feature."""
        irc, received = self.run_transfer(resume_at=RESUME_AT)

        self.assertEqual(len(received), len(CONTENT) - RESUME_AT)
        self.assertEqual(received, CONTENT[RESUME_AT:])

    def test_the_receiver_ends_up_with_the_whole_file(self):
        """What it already had, plus what we sent, is the file - which is the
        only definition of a resume having worked."""
        _irc, received = self.run_transfer(resume_at=RESUME_AT)

        self.assertEqual(CONTENT[:RESUME_AT] + received, CONTENT)

    def test_the_accept_names_the_same_byte_it_seeks_to(self):
        """An ACCEPT that promises one offset while the seek uses another
        corrupts the file quietly, and only in the middle."""
        irc, received = self.run_transfer(resume_at=RESUME_AT)

        self.assertEqual(len(irc.accept_positions()), 1)
        self.assertIn(f" {RESUME_AT}\x01", irc.accept_positions()[0])
        self.assertEqual(received[:8], CONTENT[RESUME_AT:RESUME_AT + 8])

    def test_a_send_with_no_resume_is_unaffected(self):
        """The control: the ordinary path must not have moved."""
        irc, received = self.run_transfer()

        self.assertEqual(received, CONTENT)
        self.assertEqual(irc.accept_positions(), [])

    def test_resuming_at_the_very_end_sends_nothing(self):
        """A receiver that already has all of it. Sending anything here would
        append a second copy of the tail."""
        _irc, received = self.run_transfer(resume_at=len(CONTENT))

        self.assertEqual(received, b"")

    def test_the_offer_is_not_left_behind(self):
        """Ports are reused. A stale entry would let the next offer on this
        port to this nick read an offset agreed for a different file."""
        self.run_transfer(resume_at=RESUME_AT)

        self.assertEqual(
            [key for key in runtime.dcc_send_offers if key[0] == USER], [])


if __name__ == "__main__":
    unittest.main()
