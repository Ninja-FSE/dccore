"""The fallback listener accepted whoever connected first during its
window - not only the operator (audit L16, #680).

The host check gates who can make the bot OPEN a listener; accept() then
took any peer, and the only check after it was is_bad_ip(). The DCC port
range is public and scanned: a scanner that reached the port inside the
LISTEN_TIMEOUT window got the banner (nick, version, platform, the rar
binary's path) and three password prompts, the single listener was gone
with it, and the operator's own connect found the port closed - their
login failed once, and the scanner's failed attempts were logged under the
operator's nick and host.

When the operator's CTCP advertised an address (ADMIN_CHAT_MODE = "listen"
answers a routable offer by listening), a peer from any other address is
dropped without a word and the listener keeps waiting for the rest of the
window. A passive offer carries no address, so the first peer is taken as
before - there is nothing to compare.
"""

import contextlib
import io
import os
import socket
import struct
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

LOOPBACK_LONG = struct.unpack("!I", socket.inet_aton("127.0.0.1"))[0]


class _OfferSink:
    def __init__(self):
        self.offers = []

    def sendall(self, data):
        self.offers.append(data.decode("utf-8", "replace"))


class ARealListener(DCCoreTestCase):
    """_listen_and_serve_locked() for real over loopback, with _serve()
    recorded rather than run and the window cut to a second."""

    def setUp(self):
        super().setUp()
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        self.set_config(DCC_PORT_START=55600, DCC_PORT_END=55620, NICKNAME="TestBot",
                        MY_IP_OR_DOCK="127.0.0.1")
        self._real_ip = dcc.get_public_ip_long
        dcc.get_public_ip_long = lambda: LOOPBACK_LONG
        self.addCleanup(setattr, dcc, "get_public_ip_long", self._real_ip)
        self.served = []
        self._real_serve = adminchat._serve
        adminchat._serve = lambda sock, peer_ip, nick, host, how: (self.served.append((peer_ip, nick)), sock.close())
        self.addCleanup(setattr, adminchat, "_serve", self._real_serve)
        self.addCleanup(setattr, adminchat, "LISTEN_TIMEOUT", adminchat.LISTEN_TIMEOUT)
        adminchat.LISTEN_TIMEOUT = 1.5
        self.irc = _OfferSink()
        self.out = io.StringIO()

    def listen(self, expected_ip):
        done = threading.Event()

        def run():
            with contextlib.redirect_stdout(self.out):
                adminchat._listen_and_serve_locked(self.irc, "operator", "op!u@allowed.host",
                                                   "TOKEN1", expected_ip)
            done.set()
        threading.Thread(target=run, daemon=True).start()
        deadline = threading.Event()
        for _ in range(200):
            if self.irc.offers:
                break
            deadline.wait(0.01)
        self.assertTrue(self.irc.offers, "no DCC CHAT offer went out")
        port = int(self.irc.offers[0].split("DCC CHAT chat ")[1].split()[1])
        return port, done

    def connect(self, port):
        peer = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.addCleanup(peer.close)
        return peer

    def test_the_operator_at_the_advertised_address_is_served(self):
        port, done = self.listen(expected_ip="127.0.0.1")

        self.connect(port)
        self.assertTrue(done.wait(5))

        self.assertEqual(self.served, [("127.0.0.1", "operator")])

    def test_a_stranger_gets_nothing_and_the_window_stays_open(self):
        """The audit's probe, with the offer made to another address: a
        plain socket reaching the port gets no banner and is closed, and
        the listener is still there for the operator."""
        port, done = self.listen(expected_ip="203.0.113.9")

        stranger = self.connect(port)
        self.assertEqual(stranger.recv(4096), b"", "the stranger was given the banner")
        self.assertFalse(done.is_set(), "the listener gave up after the stranger")
        second = self.connect(port)  # still listening
        self.assertEqual(second.recv(4096), b"")
        self.assertTrue(done.wait(5), "the window never closed")

        self.assertEqual(self.served, [], "somebody other than the operator was served")
        self.assertIn("Dropped a connection from 127.0.0.1", self.out.getvalue())
        self.assertIn("offered to operator at 203.0.113.9", self.out.getvalue())
        self.assertIn("did not accept the DCC CHAT offer", self.out.getvalue())

    def test_a_passive_offer_has_no_address_to_compare_and_takes_the_first_peer(self):
        port, done = self.listen(expected_ip=None)

        self.connect(port)
        self.assertTrue(done.wait(5))

        self.assertEqual(self.served, [("127.0.0.1", "operator")])


class TheListenModeBranchHandsTheAddressOn(unittest.TestCase):

    def test_the_thread_is_started_with_the_ctcps_ip(self):
        body = io.open(os.path.join(REPO_ROOT, "adminchat.py"), encoding="utf-8").read()
        branch = body[body.index("ADMIN_CHAT_MODE is 'listen'; offering the connection"):][:700]

        self.assertIn("args=(irc_sock, nick, host, token, ip)", branch)


if __name__ == "__main__":
    unittest.main()
