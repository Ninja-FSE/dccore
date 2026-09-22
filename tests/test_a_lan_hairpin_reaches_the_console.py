"""The console listener's exact-address match rejected the operator's own
connection on a NAT hairpin (#881).

#680 made `_listen_and_serve_locked()` accept a connection only from the
address the operator's CTCP advertised. When the operator and the bot
share one home router, the CTCP advertises the router's public IP - the
only address the client knows for itself - but the operator's own TCP
connection to the bot's listener came out the LAN side and arrived with a
private source address instead. The exact match dropped it as a stranger,
on the very setup #680 was meant to protect, and `ADMIN_CHAT_MODE =
"connect"` did not help either: dialling the same shared public IP back in
depends on the router supporting NAT hairpin/loopback, which most home
routers do not, for an arbitrary DCC port.

`_is_private_address()` widens the match: a peer is also accepted when its
source address is private (RFC1918) or link-local - never loopback, which
is not a LAN-sharing case and is what every same-machine test connection
in this suite necessarily arrives over. A peer reaching the port from the
public internet can never present a private source address unless it is
already inside the trusted network, so this does not reopen the scanner
risk #680 closed.
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

from tests.support import DCCoreTestCase  # noqa: E402

LOOPBACK_LONG = struct.unpack("!I", socket.inet_aton("127.0.0.1"))[0]


class TheHelperOnItsOwn(unittest.TestCase):

    def test_rfc1918_and_link_local_are_private(self):
        for ip in ("192.168.50.23", "10.0.0.5", "172.16.5.5", "169.254.1.1"):
            self.assertTrue(adminchat._is_private_address(ip), ip)

    def test_a_public_address_is_not(self):
        for ip in ("8.8.8.8", "1.1.1.1"):
            self.assertFalse(adminchat._is_private_address(ip), ip)

    def test_a_documentation_range_address_counts_as_private_too(self):
        """Python's is_private covers the IANA special-use ranges, not only
        RFC1918 - 203.0.113.0/24 (TEST-NET-3) among them. Never routable on
        a real network either way, so this widens nothing a real scanner
        could ever present."""
        self.assertTrue(adminchat._is_private_address("203.0.113.9"))

    def test_loopback_is_deliberately_excluded(self):
        """Not a LAN-sharing case, and every real test connection in this
        suite necessarily arrives over it - see the module docstring."""
        self.assertFalse(adminchat._is_private_address("127.0.0.1"))
        self.assertFalse(adminchat._is_private_address("::1"))

    def test_something_that_is_not_an_address_at_all_is_not_private(self):
        self.assertFalse(adminchat._is_private_address("not-an-ip"))
        self.assertFalse(adminchat._is_private_address(""))


class _OfferSink:
    def __init__(self):
        self.offers = []

    def sendall(self, data):
        self.offers.append(data.decode("utf-8", "replace"))


class TheListenerOverARealSocket(DCCoreTestCase):
    """_listen_and_serve_locked() for real over loopback, with the peer's
    reported address stubbed to a LAN one - the real listener always sees
    127.0.0.1 in this test environment, which is exactly what
    _is_private_address() must NOT treat as the hairpin case, so the
    address accept() reports is patched to the LAN address the audit's
    report described."""

    def setUp(self):
        super().setUp()
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        self.set_config(DCC_PORT_START=55630, DCC_PORT_END=55650, NICKNAME="TestBot",
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

    def listen_reporting(self, expected_ip, reported_peer_ip):
        """As test_the_listener_takes_only_the_operator's `listen()`, but
        with the accept()ed address patched to `reported_peer_ip` - the
        shared-router case the CTCP's own address field cannot represent
        over a real loopback test socket."""
        real_accept = socket.socket.accept

        def patched_accept(sock_self):
            conn, addr = real_accept(sock_self)
            return conn, (reported_peer_ip, addr[1])
        patcher = __import__("unittest.mock", fromlist=["mock"]).patch.object(
            socket.socket, "accept", patched_accept)
        patcher.start()
        self.addCleanup(patcher.stop)

        done = threading.Event()

        def run():
            with contextlib.redirect_stdout(self.out):
                adminchat._listen_and_serve_locked(self.irc, "operator", "op!u@allowed.host",
                                                   "TOKEN1", expected_ip)
            done.set()
        threading.Thread(target=run, daemon=True).start()
        for _ in range(200):
            if self.irc.offers:
                break
            threading.Event().wait(0.01)
        self.assertTrue(self.irc.offers, "no DCC CHAT offer went out")
        port = int(self.irc.offers[0].split("DCC CHAT chat ")[1].split()[1])
        return port, done

    def connect(self, port):
        peer = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.addCleanup(peer.close)
        return peer

    def test_a_lan_address_that_does_not_match_the_advertised_one_is_still_served(self):
        """The audit's report: the CTCP advertised the shared public IP,
        and the connection arrived from the operator's private one."""
        port, done = self.listen_reporting(expected_ip="203.0.113.50",
                                           reported_peer_ip="192.168.50.23")

        self.connect(port)
        self.assertTrue(done.wait(5))

        self.assertEqual(self.served, [("192.168.50.23", "operator")])

    def test_a_public_address_that_does_not_match_is_still_dropped(self):
        """The control: widening to private addresses must not widen to
        every address - a stranger on the public internet is still refused."""
        port, done = self.listen_reporting(expected_ip="203.0.113.50",
                                           reported_peer_ip="8.8.8.8")

        stranger = self.connect(port)
        self.assertEqual(stranger.recv(4096), b"", "the stranger was given the banner")
        self.assertFalse(done.is_set(), "the listener gave up after the stranger")

        second = self.connect(port)
        self.assertEqual(second.recv(4096), b"")
        self.assertTrue(done.wait(5), "the window never closed")
        self.assertEqual(self.served, [], "a public stranger was served")


if __name__ == "__main__":
    unittest.main()
