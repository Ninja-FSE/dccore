"""#430: dcc.py dispatches through the LIVE connection, not a stale one.

Every dispatch path in dcc.py threaded the IRC socket through as a plain
parameter - user_queue_timer captures it and waits up to 300s before using
it, inline_rar_packer can spend minutes packing an album first, and none of
them re-checked whether a reconnect had since torn down that socket and
opened a new one. A thread armed before a reconnect kept dispatching through
the dead one: the handshake send failed silently (caught, printed, ignored),
execution fell through into accept() anyway, blocked for the full 30-second
listener timeout waiting for a receiver who was never told to connect, and
three of those deleted the row with "Could not send" over the very
connection that had already failed.

start_dcc_send() - the one function every dispatch path converges on - now
re-fetches sys.modules['oserve'].irc_connection at the top and uses THAT,
treating no connection at all as "hold the queue" rather than dispatching
into nothing. Independently, a handshake send that raises now returns
immediately instead of falling through into accept().
"""

import io
import os
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase, DeadSocket, RecordingSocket  # noqa: E402

# 203.0.113.x is a documentation range Python's ipaddress module (and this
# project's own is_offerable_to_strangers()) correctly refuses as private -
# 8.8.8.8 is used elsewhere in this suite for exactly that reason, matched
# here so these tests exercise the same, real "routable" branch.
ROUTABLE = "8.8.8.8"


class ANoLongerLiveConnectionHoldsTheQueue(DCCoreTestCase):
    """oserve.irc_connection is None - the bot has no connection at all right
    now, whatever socket a caller happened to carry in."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.track = os.path.join(self.tree.root, "Song.flac")
        with io.open(self.track, "w", encoding="utf-8") as handle:
            handle.write("x" * 4096)
        self.set_config(MY_IP_OR_DOCK=ROUTABLE, MAX_DCC_SLOTS=3)
        config.active_transfers.append(
            {"user": "dave", "file": "Song.flac", "bytes_sent": 0})
        config.user_processing_lock.add("dave")
        self.oserve.irc_connection = None

    def next_file(self, **extra):
        row = {"file": "Song.flac", "path": self.track}
        row.update(extra)
        return row

    def test_it_does_not_raise(self):
        dcc.start_dcc_send(RecordingSocket(), "dave", self.track, "Song.flac",
                           "#chan", self.next_file())

    def test_the_slot_is_released(self):
        dcc.start_dcc_send(RecordingSocket(), "dave", self.track, "Song.flac",
                           "#chan", self.next_file())

        self.assertEqual(
            [tx for tx in config.active_transfers
             if str(tx.get("user", "")).lower() == "dave"],
            [])

    def test_the_processing_lock_is_released(self):
        dcc.start_dcc_send(RecordingSocket(), "dave", self.track, "Song.flac",
                           "#chan", self.next_file())

        self.assertNotIn("dave", config.user_processing_lock)

    def test_no_handshake_is_attempted_on_the_stale_parameter(self):
        """The parameter is not even trusted enough to be told the bad news
        on - there is nothing live to notify anyone over."""
        stale = RecordingSocket()

        dcc.start_dcc_send(stale, "dave", self.track, "Song.flac", "#chan",
                           self.next_file())

        self.assertEqual(stale.sent, [])

    def test_a_waiting_rar_pack_is_released_too(self):
        """is_temporary_zip rows hold rar_inprogress - stranding it here
        would freeze every OTHER user's queued pack behind a dead
        connection that has nothing to do with them."""
        self.set_config(rar_inprogress=True)

        dcc.start_dcc_send(RecordingSocket(), "dave", self.track,
                           "Song.flac", "#chan",
                           self.next_file(is_temporary_zip=True))

        self.assertFalse(config.rar_inprogress)


class TheLiveSocketIsUsedNotTheStaleParameter(DCCoreTestCase):
    """The property the fix rests on: dispatch happens on whatever oserve
    reports as connected right now, even when a different socket was
    threaded in as the parameter."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.track = os.path.join(self.tree.root, "Song.flac")
        with io.open(self.track, "w", encoding="utf-8") as handle:
            handle.write("x" * 4096)
        self.set_config(MY_IP_OR_DOCK=ROUTABLE, MAX_DCC_SLOTS=3,
                        DCC_PORT_START=0, DCC_PORT_END=0)

    def test_the_handshake_goes_out_on_the_live_socket_not_the_stale_one(self):
        stale = RecordingSocket()
        live = RecordingSocket()
        self.oserve.irc_connection = live

        with self._quiet_accept_timeout():
            dcc.start_dcc_send(stale, "dave", self.track, "Song.flac",
                               "#chan", {"file": "Song.flac",
                                         "path": self.track})

        self.assertEqual(stale.sent, [],
                         "the handshake was sent on the stale, captured "
                         "socket instead of the live connection")
        self.assertIn(b"DCC SEND", live.text().encode("utf-8", "ignore"))

    def _quiet_accept_timeout(self):
        """accept() would otherwise block for real - the assertion only
        needs the handshake, sent before accept() is ever reached, so the
        listener is given an instant timeout instead of the full 30s."""
        import socket
        import contextlib

        @contextlib.contextmanager
        def guard():
            real_settimeout = socket.socket.settimeout
            real_accept = socket.socket.accept

            def short_timeout(self_sock, value):
                # Only shorten the LISTENER's 30s wait for a peer; leave
                # everything else (including None, which disables a
                # timeout) alone.
                return real_settimeout(self_sock, 0.2 if value == 30.0 else value)

            def accept_or_timeout(self_sock):
                try:
                    return real_accept(self_sock)
                except socket.timeout:
                    raise socket.timeout("no peer in this test")

            socket.socket.settimeout = short_timeout
            try:
                yield
            finally:
                socket.socket.settimeout = real_settimeout

        return guard()


class AFailedHandshakeReturnsImmediately(DCCoreTestCase):
    """Before #430's second fix, this fell through into accept() and blocked
    for the full 30-second listener timeout even though the offer never
    reached anyone - the send that would have told them to connect is
    exactly what had just failed."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.track = os.path.join(self.tree.root, "Song.flac")
        with io.open(self.track, "w", encoding="utf-8") as handle:
            handle.write("x" * 4096)
        self.set_config(MY_IP_OR_DOCK=ROUTABLE, MAX_DCC_SLOTS=3)
        config.active_transfers.append(
            {"user": "dave", "file": "Song.flac", "bytes_sent": 0})
        self.oserve.irc_connection = DeadSocket()

    def test_it_returns_quickly_rather_than_blocking_in_accept(self):
        started = time.perf_counter()

        dcc.start_dcc_send(DeadSocket(), "dave", self.track, "Song.flac",
                           "#chan", {"file": "Song.flac", "path": self.track})

        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 5.0,
                        "start_dcc_send blocked as though it reached "
                        "accept() after a handshake that never went out")

    def test_the_slot_is_still_released(self):
        dcc.start_dcc_send(DeadSocket(), "dave", self.track, "Song.flac",
                           "#chan", {"file": "Song.flac", "path": self.track})

        self.assertEqual(
            [tx for tx in config.active_transfers
             if str(tx.get("user", "")).lower() == "dave"],
            [])


if __name__ == "__main__":
    unittest.main()
