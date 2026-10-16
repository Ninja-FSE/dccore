"""#1330: the NICK, WHO, NAMES, JOIN, PART and QUIT handlers, called directly.

They were branches of irc_loop(), reachable only by driving the whole loop
against a scripted server (test_audit_irc_connection.py and others still do,
for the wiring). Step 4 of splitting the loop made them functions; here each
is handed one line and its effect on the channel lists is read back.

Every nick, channel and host here is invented.
"""

import contextlib
import io
import os
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

SERVER = ":irc.example.net"


class RecordedThread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, tuple(args)

    def start(self):
        RecordedThread.started.append((self.target, self.args))


class ChannelHandlers(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="alfa", ORIGINAL_NICK="alfa", CHANNEL="#somechannel")
        config.channel_users.clear()
        config.frozen_queues.clear()
        config.dcc_queue.clear()
        silence_debug(announce)
        RecordedThread.started = []
        # A stub module in irc's place, as the loop-driving tests do: patching
        # threading.Thread itself would reach every module in the process.
        stub = types.ModuleType("threading")
        stub.__dict__.update(threading.__dict__)
        stub.Thread = RecordedThread
        real = irc.threading
        irc.threading = stub
        self.addCleanup(setattr, irc, "threading", real)
        self.sock = object()

    def members(self, chan="#somechannel"):
        return sorted(config.channel_users.get(chan, ()))

    def quietly(self, call, *args):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            call(*args)
        return said.getvalue()


class TheMemberLists(ChannelHandlers):

    def test_names_learns_who_is_there(self):
        self.quietly(irc.answer_names, SERVER + " 353 alfa = #somechannel :alfa @bravo +charlie", self.sock)
        self.assertEqual(self.members(), ["alfa", "bravo", "charlie"])

    def test_names_thaws_a_frozen_queue_whose_owner_is_still_there(self):
        config.frozen_queues["bravo"] = 1.0
        config.dcc_queue["bravo"] = [{"file": "a.flac"}]

        said = self.quietly(irc.answer_names, SERVER + " 353 alfa = #somechannel :bravo", self.sock)

        self.assertNotIn("bravo", config.frozen_queues)
        self.assertIn("RECONNECT THAW", said)
        self.assertEqual([args for _target, args in RecordedThread.started], [(self.sock, "bravo")],
                         "the queue is restarted on this connection's socket")

    def test_a_names_line_naming_no_valid_target_changes_nothing(self):
        self.quietly(irc.answer_names, SERVER + " 353 alfa = #bad,chan :bravo", self.sock)
        self.assertEqual(config.channel_users, {})

    def test_a_join_adds_the_member_and_a_part_removes_them(self):
        self.quietly(irc.answer_join, ":bravo!b@host.example JOIN #somechannel", self.sock)
        self.assertEqual(self.members(), ["bravo"])

        self.quietly(irc.answer_part, ":bravo!b@host.example PART #somechannel :bye")
        self.assertEqual(self.members(), [])

    def test_a_join_thaws_the_joiners_queue_on_this_socket(self):
        config.frozen_queues["bravo"] = 1.0
        self.quietly(irc.answer_join, ":bravo!b@host.example JOIN #somechannel", self.sock)
        self.assertEqual([args for _target, args in RecordedThread.started], [(self.sock, "bravo")])

    def test_a_quit_removes_the_member_from_every_channel(self):
        config.channel_users.update({"#somechannel": {"bravo", "charlie"}, "#other": {"bravo"}})

        self.quietly(irc.answer_quit, ":bravo!b@host.example QUIT :gone")

        self.assertEqual(self.members(), ["charlie"])
        self.assertEqual(self.members("#other"), [])

    def test_a_nick_change_follows_the_member(self):
        config.channel_users["#somechannel"] = {"bravo"}

        self.quietly(irc.answer_nick_change, ":bravo!b@host.example NICK :bravo2")

        self.assertEqual(self.members(), ["bravo2"])


class TheWhoReply(ChannelHandlers):

    def test_it_is_bounded(self):
        real = irc.WHOIS_STATUS_MAX
        irc.WHOIS_STATUS_MAX = 2
        self.addCleanup(setattr, irc, "WHOIS_STATUS_MAX", real)
        self.set_config(whois_status={})

        for nick in ("bravo", "charlie", "delta"):
            self.quietly(irc.answer_who_reply,
                         SERVER + " 352 alfa #somechannel %s host.example irc.example.net %s H :0 Someone" % (nick, nick))

        self.assertLessEqual(len(config.whois_status), 2)


if __name__ == "__main__":
    unittest.main()
