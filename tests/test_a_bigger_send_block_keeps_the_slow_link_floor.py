"""A bigger send block keeps the slow-link floor, and 256 KB is on the menu.

#1139, from the performance audit. Every block of a DCC send costs one read,
one sendall() and one check for acks, so a 1 GB file at 64 KB is 16,384
rounds of them. On loopback a 256 KB block measured 30-40% less sender CPU per
GB than 64 KB, so 256 KB is now a choice on the Settings page for a fast
seedbox.

It is NOT the default, and the default is pinned here at 64 KB on purpose. The
live speed ("Speed now" on the dashboard, "Speed:" in the channel advert) is
sampled once a second from bytes_sent, which moves one whole block at a time.
At 256 KB a transfer slower than about 256 KB/s reads 0 in most samples and a
one-block jump in the rest, and home-hosted transfers of 20-500 KB/s are
normal. Measured at real link speeds, the CPU saving was about 1% of a core at
100 Mbps - not worth a public advert that says 0 mid-transfer.

The catch the audit found in choosing a bigger block: CPython applies a
socket timeout to the WHOLE of a sendall() call, not to each send() inside it.
With the timeout left at 60 s, the slowest receiver a send survives is
block / 60 s - 1.1 KB/s at 64 KB but 4.4 KB/s at 256 KB, so picking the bigger
block would quietly start dropping slow receivers that work at 64 KB. So the
timeout scales with the block above 64 KB, and stays exactly 60 s at 64 KB
and below.
"""

import ast
import io
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import runtime  # noqa: E402

from tests import dcc_ports  # noqa: E402
from tests.support import DCCoreTestCase, parse_source  # noqa: E402
from tests.test_dcc_resume_end_to_end import RecordingIrcSocket, loopback_is_usable  # noqa: E402

USER = "someuser"
PORT_START, PORT_END = dcc_ports(51360, 51370)
CONTENT = bytes(range(256)) * 2048          # 524,288 bytes: two 256 KB blocks
OLD_FLOOR = 65536 / 60.0                    # bytes per second a send survives at 64 KB


def shipped_default():
    """DCC_BLOCK_SIZE as written in defaults.py, not as a test left it."""
    path = os.path.join(REPO_ROOT, "src", "defaults.py")
    with io.open(path, encoding="utf-8") as handle:
        tree = parse_source(handle.read())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "DCC_BLOCK_SIZE":
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DCC_BLOCK_SIZE"
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("DCC_BLOCK_SIZE is not assigned in defaults.py")


class TheDefaultBlock(DCCoreTestCase):

    def test_the_shipped_default_stays_64_kb(self):
        """Not 256 KB, though it costs less CPU: the live speed moves in steps
        of one block, so a bigger default would make the public advert read 0
        for a slow transfer that is still moving. See the module docstring."""
        self.assertEqual(shipped_default(), 65536)

    def test_the_fallback_is_the_shipped_default(self):
        """Two defaults would disagree about what an operator who never chose
        gets."""
        self.assertEqual(dcc.DEFAULT_DCC_BLOCK_SIZE, shipped_default())

    def test_an_unreadable_setting_falls_back_to_it(self):
        self.set_config(DCC_BLOCK_SIZE="as big as it goes")

        self.assertEqual(dcc.dcc_block_size(), shipped_default())

    def test_the_default_is_on_the_menu(self):
        """A default the menu does not offer shows as the first choice, 4 KB,
        and saving the page would silently write it back."""
        import settings_file

        self.assertIn(str(shipped_default()), settings_file.CHOICES["DCC_BLOCK_SIZE"])

    def test_256_kb_is_on_the_menu_with_a_label(self):
        import settings_file
        import webserver

        self.assertIn("262144", settings_file.CHOICES["DCC_BLOCK_SIZE"])
        self.assertEqual(webserver.CHOICE_LABELS["DCC_BLOCK_SIZE"]["262144"], "256 KB")

    def test_the_old_choices_are_all_still_offered(self):
        import settings_file

        for value in ("4096", "8192", "16384", "32768", "65536", "131072"):
            with self.subTest(value=value):
                self.assertIn(value, settings_file.CHOICES["DCC_BLOCK_SIZE"])


class TheSendTimeout(unittest.TestCase):

    def test_up_to_64_kb_it_is_the_minute_it_always_was(self):
        for block in (4096, 8192, 16384, 32768, 65536):
            with self.subTest(block=block):
                self.assertEqual(dcc._send_timeout(block), 60.0)

    def test_above_64_kb_it_grows_in_proportion(self):
        self.assertEqual(dcc._send_timeout(131072), 120.0)
        self.assertEqual(dcc._send_timeout(262144), 240.0)
        self.assertEqual(dcc._send_timeout(dcc.MAX_DCC_BLOCK_SIZE), 960.0)

    def test_the_slowest_link_that_survives_does_not_rise(self):
        """block / timeout is the slowest receiver one sendall() survives. It
        must not rise above what 64 KB a minute allows, at any size."""
        for block in (4096, 16384, 65536, 131072, 196608, 262144,
                      dcc.MAX_DCC_BLOCK_SIZE):
            with self.subTest(block=block):
                self.assertLessEqual(block / dcc._send_timeout(block), OLD_FLOOR + 1e-9)

    def test_the_transfer_sets_it_after_the_block_is_known(self):
        """Read out of the source as well as driven below, because the real
        transfer needs a loopback listener some runners cannot open. The
        statement must sit between resolving the block and the loop that
        sends it."""
        with io.open(os.path.join(REPO_ROOT, "src", "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()
        after_block = code.split("            block = dcc_block_size()\n", 1)[1]
        before_loop = after_block.split("            while True:\n", 1)[0]

        self.assertIn("            conn.settimeout(_send_timeout(block))\n", before_loop)


@unittest.skipUnless(loopback_is_usable(),
                     "this runner cannot bind a listener and dial loopback")
class ARealTransfer(DCCoreTestCase):
    """The bot on one side of a loopback socket, this test on the other, with
    every settimeout() the transfer makes recorded."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-block-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.served = os.path.join(self.tmp, "Some_Album.zip")
        with io.open(self.served, "wb") as handle:
            handle.write(CONTENT)
        self.set_config(
            active_transfers=[{"user": USER, "file": "Some_Album.zip",
                               "bytes_sent": 0, "next_file_obj": "Some_Album.zip"}],
            MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8",
            DCC_PORT_START=PORT_START, DCC_PORT_END=PORT_END,
            # Below a minute, so the listener's own timeout cannot be
            # mistaken for the data connection's.
            DCC_ACCEPT_TIMEOUT=30)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)

        self.debug_lines = []
        real_send_debug = announce.send_debug
        announce.send_debug = lambda text, category="INFO": self.debug_lines.append((category, text))
        self.addCleanup(setattr, announce, "send_debug", real_send_debug)
        self.oserve.total_sent_bytes = 0

        # Every timeout set on a socket while the transfer runs. The test's own
        # client sets 20 s and the listener sets its accept window; only the
        # values the data connection could carry are asserted on.
        self.timeouts = []
        real_settimeout = socket.socket.settimeout
        timeouts = self.timeouts

        def recording_settimeout(sock, value):
            timeouts.append(value)
            return real_settimeout(sock, value)

        # socket.socket inherits settimeout from the C base class, so the
        # cleanup removes the stand-in rather than pinning a copy on the class.
        had_its_own = "settimeout" in vars(socket.socket)
        socket.socket.settimeout = recording_settimeout
        if had_its_own:
            self.addCleanup(setattr, socket.socket, "settimeout", real_settimeout)
        else:
            self.addCleanup(delattr, socket.socket, "settimeout")

    def send_and_receive(self):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, USER, self.served, "Some_Album.zip", "#somechannel", "Some_Album.zip"),
            daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc.handshake_seen.wait(20), "no DCC SEND handshake")
        client = socket.create_connection(("127.0.0.1", irc.port()), timeout=20)
        self.addCleanup(client.close)
        held = 0
        received = bytearray()
        while held < len(CONTENT):
            chunk = client.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
            held += len(chunk)
            client.sendall(struct.pack("!I", held & 0xFFFFFFFF))
        sender.join(30)
        self.assertFalse(sender.is_alive(), "the send never finished")
        self.assertEqual(bytes(received), CONTENT)
        sent = [t for c, t in self.debug_lines if t.startswith("Sent:")]
        self.assertEqual(len(sent), 1, self.debug_lines)

    def test_the_default_block_keeps_the_minute(self):
        self.send_and_receive()

        self.assertIn(60.0, self.timeouts)
        self.assertEqual([t for t in self.timeouts if t is not None and t > 60.0], [],
                         self.timeouts)

    def test_a_256_kb_block_sends_with_a_four_minute_timeout(self):
        self.set_config(DCC_BLOCK_SIZE=262144)

        self.send_and_receive()

        self.assertIn(240.0, self.timeouts)

    def test_a_128_kb_block_gets_two_minutes(self):
        self.set_config(DCC_BLOCK_SIZE=131072)

        self.send_and_receive()

        self.assertIn(120.0, self.timeouts)
        self.assertNotIn(240.0, self.timeouts)


if __name__ == "__main__":
    unittest.main()
