"""Auto mode's fallback listener took the first peer from anywhere.

ADMIN_CHAT_MODE = "auto" (the default) dials the address the operator's DCC
CHAT offer advertised and, when that dial fails - the common case behind
NAT - listens and offers the connection back. #680 made the listener take
only the advertised address, but only on listen mode's path: the fallback
called the listener without it, so a scanner that reached the offered port
inside the window got the banner and the password prompts, and the
operator's own connect found the port gone.

The fallback now passes an address as well. The one the offer advertised -
unless that is a LAN address, which a client behind NAT reports when it does
not know its public one, and whose connection then comes from its router's
public address: then the IRC host's, when that is an address. Neither: the
first peer, as for a passive offer (said in the log).

Driven through the real _connect_and_serve() over loopback, with the dial
refused and _serve() recorded rather than run.
"""

import contextlib
import io
import socket
import struct
import sys
import os
import threading

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import dcc  # noqa: E402

from tests import dcc_ports  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402

LOOPBACK_LONG = struct.unpack("!I", socket.inet_aton("127.0.0.1"))[0]


class _OfferSink:
    def __init__(self):
        self.offers = []

    def sendall(self, data):
        self.offers.append(data.decode("utf-8", "replace"))


class _RefusingSocketModule:
    """adminchat's `socket`, with every dial refused and the rest real."""

    def __getattr__(self, name):
        return getattr(socket, name)

    @staticmethod
    def create_connection(address, timeout=None, *args, **kwargs):
        raise ConnectionRefusedError(f"refused (test): {address[0]}")


class WhoTheFallbackWaitsFor(DCCoreTestCase):

    def test_a_public_advertised_address_is_the_one(self):
        self.assertEqual(adminchat._fallback_expected_ip("203.0.113.41", "alfa.host.example"),
                         "203.0.113.41")

    def test_a_lan_address_defers_to_an_irc_host_that_is_an_address(self):
        for lan in ("192.168.1.20", "10.1.2.3", "172.20.0.5", "169.254.9.9", "100.64.3.4"):
            with self.subTest(lan=lan):
                self.assertEqual(adminchat._fallback_expected_ip(lan, "198.51.100.7"),
                                 "198.51.100.7")

    def test_a_lan_address_and_a_host_name_say_nothing(self):
        self.assertIsNone(adminchat._fallback_expected_ip("192.168.1.20", "alfa.host.example"))
        self.assertIsNone(adminchat._fallback_expected_ip("192.168.1.20", None))

    def test_loopback_is_the_operators_own_machine_and_kept(self):
        self.assertEqual(adminchat._fallback_expected_ip("127.0.0.1", "alfa.host.example"),
                         "127.0.0.1")


class AutoModesFallbackOverLoopback(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        port_start, port_end = dcc_ports(55640, 55660)
        self.set_config(DCC_PORT_START=port_start, DCC_PORT_END=port_end, NICKNAME="TestBot",
                        MY_IP_OR_DOCK="127.0.0.1", ADMIN_CHAT_MODE="auto")
        real_ip = dcc.get_public_ip_long
        dcc.get_public_ip_long = lambda: LOOPBACK_LONG
        self.addCleanup(setattr, dcc, "get_public_ip_long", real_ip)
        real_socket = adminchat.socket
        adminchat.socket = _RefusingSocketModule()
        self.addCleanup(setattr, adminchat, "socket", real_socket)
        self.served = []
        real_serve = adminchat._serve
        adminchat._serve = lambda sock, peer_ip, nick, host, how: (
            self.served.append((peer_ip, nick)), sock.close())
        self.addCleanup(setattr, adminchat, "_serve", real_serve)
        self.addCleanup(setattr, adminchat, "LISTEN_TIMEOUT", adminchat.LISTEN_TIMEOUT)
        adminchat.LISTEN_TIMEOUT = 1.5
        self.irc = _OfferSink()
        self.out = io.StringIO()

    def fall_back(self, advertised_ip, host):
        done = threading.Event()

        def run():
            with contextlib.redirect_stdout(self.out):
                adminchat._connect_and_serve(self.irc, "alfa", host, advertised_ip, 55101)
            done.set()
        threading.Thread(target=run, daemon=True).start()
        waiting = threading.Event()
        for _ in range(500):
            if self.irc.offers:
                break
            waiting.wait(0.01)
        self.assertTrue(self.irc.offers, "the failed dial did not fall back to an offer")
        port = int(self.irc.offers[0].split("DCC CHAT chat ")[1].split()[1].strip("\x01"))
        return port, done

    def connect(self, port):
        peer = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.addCleanup(peer.close)
        return peer

    def test_a_stranger_gets_nothing_when_the_offer_named_another_address(self):
        """The audit's probe: the offer said 203.0.113.41, the dial was
        refused, and a peer from 127.0.0.1 reaches the offered port."""
        port, done = self.fall_back("203.0.113.41", "alfa.host.example")

        stranger = self.connect(port)
        self.assertEqual(stranger.recv(4096), b"", "the stranger was given the banner")
        self.assertTrue(done.wait(5), "the window never closed")

        self.assertEqual(self.served, [], "auto mode's fallback served a stranger")
        self.assertIn("Dropped a connection from 127.0.0.1", self.out.getvalue())
        self.assertIn("offered to alfa at 203.0.113.41", self.out.getvalue())

    def test_the_operator_at_the_advertised_address_is_served(self):
        port, done = self.fall_back("127.0.0.1", "alfa.host.example")

        self.connect(port)
        self.assertTrue(done.wait(5))

        self.assertEqual(self.served, [("127.0.0.1", "alfa")])

    def test_a_lan_offer_is_served_from_the_irc_hosts_address(self):
        port, done = self.fall_back("192.168.7.20", "127.0.0.1")

        self.connect(port)
        self.assertTrue(done.wait(5))

        self.assertEqual(self.served, [("127.0.0.1", "alfa")])

    def test_a_lan_offer_refuses_a_peer_that_is_not_the_irc_host(self):
        port, done = self.fall_back("192.168.7.20", "198.51.100.7")

        stranger = self.connect(port)
        self.assertEqual(stranger.recv(4096), b"")
        self.assertTrue(done.wait(5))

        self.assertEqual(self.served, [])

    def test_with_nothing_to_compare_the_first_peer_is_taken_and_the_log_says_so(self):
        port, done = self.fall_back("192.168.7.20", "alfa.host.example")

        self.connect(port)
        self.assertTrue(done.wait(5))

        self.assertEqual(self.served, [("127.0.0.1", "alfa")])
        self.assertIn("takes the first connection", self.out.getvalue())


if __name__ == "__main__":
    import unittest
    unittest.main()
