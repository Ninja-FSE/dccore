"""A failure setting up the listener must not cost a DCC slot for ever.

The caller appends the transfer to config.active_transfers BEFORE calling
start_dcc_send() - all three dispatch sites in check_queue_and_send() do, and
so does the direct path. Only the finally: of one try inside start_dcc_send
removes it again.

settimeout() and listen() sat ABOVE that try. An OSError out of either -
EMFILE when the process has run out of file descriptors, or the kernel
refusing the backlog - killed the dispatch thread with the row still in the
list and the listener socket still open.

Nothing ever revisits such a row. It names a transfer that is not happening,
and no completion will fire to remove it, so it is one permanent slot out of
MAX_DCC_SLOTS, cumulative, until the daemon restarts. The conditions that make
listen() fail are exactly the ones where losing serving capacity hurts most.

WHY listen() DID NOT SIMPLY MOVE DOWN

The handshake is what tells the peer to connect, so a peer dialling before we
listen gets a refusal. listen() has to stay ahead of it, which is why the
whole block moved inside the try instead.
"""

import os
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class AListenerThatCannotBeSetUp(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.served = os.path.join(self.tree.music, "track.flac")
        os.makedirs(os.path.dirname(self.served), exist_ok=True)
        with open(self.served, "wb") as handle:
            handle.write(b"x" * 512)
        # NOT a documentation-range address. 192.0.2.x, 198.51.100.x and
        # 203.0.113.x are RESERVED, and is_offerable_to_strangers() refuses to
        # offer from one - correctly, since nobody can dial it. The first
        # version of this fixture used TEST-NET-3 and never reached listen()
        # at all, so all four tests failed for a reason unrelated to the one
        # they exist to check.
        self.set_config(active_transfers=[{"user": "alice", "file": "track.flac",
                                           "bytes_sent": 0,
                                           "next_file_obj": "track.flac"}],
                        MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8",
                        DCC_PORT_START=51000, DCC_PORT_END=51010)

    def break_listen(self):
        """Make listen() raise the way an out-of-descriptors host does."""
        real_listen = socket.socket.listen

        def exploding(self_sock, *args):
            raise OSError(24, "Too many open files")

        socket.socket.listen = exploding
        self.addCleanup(lambda: setattr(socket.socket, "listen", real_listen))

    def start(self):
        """start_dcc_send runs on a thread in production; a raise there is
        silent. Called directly so the test sees what the thread would.

        #430: start_dcc_send() now re-fetches the LIVE socket from
        sys.modules['oserve'].irc_connection rather than trusting whatever
        was threaded through as a parameter, and holds the queue instead of
        dispatching when there isn't one - so the fake here has to be the
        one oserve reports as live, or every call below bails before
        reaching any of the behaviour these tests exist to check.
        """
        sock = FakeIrcSocket()
        self.oserve.irc_connection = sock
        try:
            dcc.start_dcc_send(sock, "alice", self.served,
                               "track.flac", "#chan", "track.flac")
        except Exception:
            # The defect is the leaked slot, not whether it propagates.
            pass

    def test_the_slot_is_released_when_listen_fails(self):
        self.break_listen()

        self.start()

        self.assertEqual(
            [tx for tx in config.active_transfers
             if str(tx.get("user", "")).lower() == "alice"],
            [],
            "the slot stayed claimed for a transfer that never started - "
            "nothing will ever revisit that row")

    def test_it_does_not_leak_the_listening_socket(self):
        """The same finally closes the socket. A descriptor leak on the one
        path that fires when descriptors have run out."""
        opened = []
        real_close = socket.socket.close

        def counted_close(self_sock):
            opened.append(self_sock)
            return real_close(self_sock)

        socket.socket.close = counted_close
        self.addCleanup(lambda: setattr(socket.socket, "close", real_close))
        self.break_listen()

        self.start()

        self.assertTrue(opened, "the listener was never closed")

    def test_a_working_listen_still_serves(self):
        """Control. Every assertion above passes just as happily against a
        start_dcc_send() that refuses to do anything at all, so this pins that
        the ordinary path still reaches the accept()."""
        accepted = []
        real_accept = socket.socket.accept

        def record_and_stop(self_sock):
            accepted.append(True)
            raise socket.timeout("no peer in this test")

        socket.socket.accept = record_and_stop
        self.addCleanup(lambda: setattr(socket.socket, "accept", real_accept))

        self.start()

        self.assertTrue(accepted,
                        "the ordinary path no longer reaches accept()")

    def test_the_handshake_is_still_sent_after_listen(self):
        """Ordering is load-bearing: the handshake tells the peer to connect,
        so a peer dialling before listen() gets a refusal. Moving the calls
        down instead of moving the block in would have broken this."""
        sock = FakeIrcSocket()
        self.oserve.irc_connection = sock
        real_accept = socket.socket.accept
        real_listen = socket.socket.listen
        order = []

        def noting_listen(self_sock, *args):
            order.append("listen")
            return real_listen(self_sock, *args)

        def noting_accept(self_sock):
            order.append("accept")
            raise socket.timeout("no peer in this test")

        socket.socket.listen = noting_listen
        socket.socket.accept = noting_accept
        self.addCleanup(lambda: setattr(socket.socket, "listen", real_listen))
        self.addCleanup(lambda: setattr(socket.socket, "accept", real_accept))

        try:
            dcc.start_dcc_send(sock, "alice", self.served, "track.flac",
                               "#chan", "track.flac")
        except Exception:
            pass

        handshake_at = next((n for n, sent in enumerate(sock.sent)
                             if b"DCC SEND" in sent), None)
        self.assertIsNotNone(handshake_at, "no handshake was sent")
        self.assertEqual(order[0], "listen",
                         "the handshake path ran before the socket was "
                         "listening; a peer dialling that fast is refused")


class FakeIrcSocket:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return len(payload)

    def sendall(self, payload):
        # Delegated, not duplicated (#504). A stand-in that behaves
        # differently depending on which call production used is a place a
        # regression can hide.
        self.send(payload)


if __name__ == "__main__":
    unittest.main()
