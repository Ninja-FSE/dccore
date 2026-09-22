"""A receiver that never connects is reported as that (#879).

A night's console feed showed a run of

    Failed: "<file>" to <nick> - the send blocked for the whole socket
    timeout with the receiver not draining it. (0B of 0B arrived)

between transfers that completed at 3 MB/s to other people. Nothing was
wrong with the link: nobody had connected. start_dcc_send() listened with
a 30 s timeout and its accept() raised socket.timeout into the send loop's
own `except socket.timeout`, whose wording describes a send that stalled -
and report_failure() was called without the byte counts, so the feed said
"0B of 0B". The accept timeout has its own branch now and says what it is,
with the file size; the window is DCC_ACCEPT_TIMEOUT, on the Transfers
page, because a person who has to click Accept often needs more than 30 s.

Three layers: the helpers on their own; the whole send path against a fake
socket whose accept() times out, which runs on every runner; and the same
against a real loopback listener that nobody dials, where a listener can be
bound.
"""

import io
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_dcc_resume_end_to_end import RecordingIrcSocket, loopback_is_usable  # noqa: E402

USER = "someuser"
FILE = "Some_Album.zip"
CONTENT = bytes(range(256)) * 40          # 10,240 bytes
PORT_START = 51320
PORT_END = 51330


class TheWindow(unittest.TestCase):

    def setUp(self):
        self.had = getattr(config, "DCC_ACCEPT_TIMEOUT", None)
        self.addCleanup(setattr, config, "DCC_ACCEPT_TIMEOUT", self.had)

    def test_the_setting_is_what_the_listener_waits(self):
        config.DCC_ACCEPT_TIMEOUT = 90
        self.assertEqual(dcc.accept_timeout(), 90.0)

    def test_the_default_is_the_thirty_seconds_it_always_was(self):
        self.assertEqual(config.DCC_ACCEPT_TIMEOUT, 30)

    def test_a_zero_or_a_typo_cannot_make_every_offer_fail_at_once(self):
        config.DCC_ACCEPT_TIMEOUT = 0
        self.assertEqual(dcc.accept_timeout(), 1.0)
        config.DCC_ACCEPT_TIMEOUT = "soon"
        self.assertEqual(dcc.accept_timeout(), 30.0)

    def test_the_reason_says_nobody_came_not_that_a_send_stalled(self):
        reason = dcc.never_connected_reason(45.0)
        self.assertIn("never connected within 45s", reason)
        self.assertIn("not accepted, or their client ignored it", reason)
        self.assertNotIn("draining", reason)


class _NobodyEverConnects:
    """socket.socket for a listener that binds, listens, and waits in vain.

    The window it was told to wait is recorded, and accept() raises the
    timeout the real one would after that long - without the wait.
    """

    windows = []

    def __init__(self, *args, **kwargs):
        self.timeout = None

    def setsockopt(self, *args):
        pass

    def bind(self, address):
        pass

    def settimeout(self, value):
        self.timeout = value
        if value is not None:
            _NobodyEverConnects.windows.append(value)

    def listen(self, backlog):
        pass

    def accept(self):
        raise socket.timeout("timed out")

    def close(self):
        pass


class NobodyConnects(DCCoreTestCase):
    """The whole send path, with the receiver replaced by silence."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-accept-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.served = os.path.join(self.tmp, FILE)
        with io.open(self.served, "wb") as handle:
            handle.write(CONTENT)
        self.set_config(
            active_transfers=[{"user": USER, "file": FILE, "bytes_sent": 0, "next_file_obj": FILE}],
            MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8",
            DCC_PORT_START=PORT_START, DCC_PORT_END=PORT_END,
            DCC_ACCEPT_TIMEOUT=45)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        self.events = []
        patch = mock.patch.object(announce, "feed_event",
                                  lambda kind, text, **fields: self.events.append((kind, text, fields)))
        patch.start()
        self.addCleanup(patch.stop)
        _NobodyEverConnects.windows = []
        patch = mock.patch("socket.socket", _NobodyEverConnects)
        patch.start()
        self.addCleanup(patch.stop)
        self.oserve.send_fails_count = 0

    def run_the_send(self):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        with mock.patch("time.sleep", lambda *_a, **_k: None):
            dcc.start_dcc_send(irc, USER, self.served, FILE, "#somechannel", FILE)
        return irc

    def failures(self):
        return [(text, fields) for kind, text, fields in self.events if kind == "FAIL"]

    def test_it_is_reported_as_never_connected_with_the_file_size(self):
        irc = self.run_the_send()

        self.assertTrue(irc.handshake_seen.is_set(), "the offer went out")
        self.assertEqual(len(self.failures()), 1, self.events)
        text, fields = self.failures()[0]
        self.assertIn("never connected within 45s", text)
        self.assertNotIn("draining", text)
        self.assertEqual(fields.get("acked"), 0)
        self.assertEqual(fields.get("total"), len(CONTENT),
                         "the feed says '0B of <size>', not '0B of 0B'")

    def test_the_listener_waited_the_configured_window(self):
        self.run_the_send()
        self.assertIn(45.0, _NobodyEverConnects.windows)

    def test_the_slot_and_the_offer_are_released_and_the_miss_is_counted(self):
        self.run_the_send()
        self.assertEqual(config.active_transfers, [])
        self.assertEqual(runtime.dcc_send_offers, {})
        self.assertEqual(self.oserve.send_fails_count, 1)


@unittest.skipUnless(loopback_is_usable(),
                     "this runner cannot bind a listener and dial loopback")
class ARealListenerNobodyDials(DCCoreTestCase):
    """The same, through a real socket, with the window shortened to 1 s."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-accept-real-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.served = os.path.join(self.tmp, FILE)
        with io.open(self.served, "wb") as handle:
            handle.write(CONTENT)
        self.set_config(
            active_transfers=[{"user": USER, "file": FILE, "bytes_sent": 0, "next_file_obj": FILE}],
            MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8",
            DCC_PORT_START=PORT_START, DCC_PORT_END=PORT_END,
            DCC_ACCEPT_TIMEOUT=1)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        self.debug_lines = []
        patch = mock.patch.object(announce, "send_debug",
                                  lambda text, category="INFO", notice=None: self.debug_lines.append((category, text)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_failure_is_the_accept_window_not_a_stalled_send(self):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, USER, self.served, FILE, "#somechannel", FILE), daemon=True)
        sender.start()
        self.assertTrue(irc.handshake_seen.wait(20), "no DCC SEND handshake")
        sender.join(30)
        self.assertFalse(sender.is_alive(), "the send did not end on the accept window")

        failed = [t for c, t in self.debug_lines if c == "FAIL"]
        self.assertEqual(len(failed), 1, self.debug_lines)
        self.assertIn("never connected within 1s", failed[0])
        self.assertNotIn("draining", failed[0])


if __name__ == "__main__":
    unittest.main()
