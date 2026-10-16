"""#1338: irc.answer_ctcp(), the CTCP branch of the PRIVMSG dispatch.

It was the first arm of answer_privmsg()'s command chain; step 7 of
splitting irc_loop() made it a function. Here it is handed one CTCP line
and what it started is read back: a DCC offer is acted on only when it was
sent privately, VERSION is answered through the oserve passed in, and the
queue commands start their handler. The last class checks that
answer_privmsg() hands a CTCP line over, and only a CTCP line.

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

import adminchat  # noqa: E402
import announce  # noqa: E402
import commands  # noqa: E402
import dcc_fetch  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase, install_fake_oserve, silence_debug  # noqa: E402

PRIVATE = "alfa"
CHANNEL = "#somechannel"


class RecordedThread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, tuple(args)

    def start(self):
        RecordedThread.started.append(self.target)


class CTCP(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="alfa", ORIGINAL_NICK="alfa", CHANNEL=CHANNEL, DEBUG_CHANNEL="")
        silence_debug(announce)
        self.oserve = install_fake_oserve()
        RecordedThread.started = []
        stub = types.ModuleType("threading")
        stub.__dict__.update(threading.__dict__)
        stub.Thread = RecordedThread
        real = irc.threading
        irc.threading = stub
        self.addCleanup(setattr, irc, "threading", real)
        self.chats = []
        real_chat = adminchat.handle_dcc_chat
        adminchat.handle_dcc_chat = lambda sock, line, user, text: self.chats.append(text)
        self.addCleanup(setattr, adminchat, "handle_dcc_chat", real_chat)

    def ctcp(self, text, to=PRIVATE):
        msg = "\x01%s\x01" % text
        line = ":bravo!b@host.example PRIVMSG %s :%s" % (to, msg)
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_ctcp(line, msg, "bravo", to, object(), self.oserve)


class DCCOffers(CTCP):

    def test_a_private_chat_offer_reaches_the_admin_console(self):
        self.ctcp("DCC CHAT chat 3232235777 5000")
        self.assertEqual(self.chats, ["DCC CHAT chat 3232235777 5000"])

    def test_one_sent_to_a_channel_does_not(self):
        self.ctcp("DCC CHAT chat 3232235777 5000", to=CHANNEL)
        self.assertEqual(self.chats, [])

    def test_a_private_send_offer_starts_the_fetch(self):
        self.ctcp("DCC SEND some.flac 3232235777 5000 1000")
        self.assertEqual(RecordedThread.started, [dcc_fetch.handle_incoming_offer])

    def test_one_sent_to_a_channel_does_not_start_it(self):
        self.ctcp("DCC SEND some.flac 3232235777 5000 1000", to=CHANNEL)
        self.assertEqual(RecordedThread.started, [])


class Replies(CTCP):

    def test_version_is_answered_through_the_passed_oserve(self):
        self.ctcp("VERSION")
        self.assertEqual(len(self.oserve.queued), 1, self.oserve.queued)
        self.assertEqual(self.oserve.queued[0][0], "bravo")

    def test_que_starts_the_queue_check(self):
        self.ctcp("QUE", to=CHANNEL)
        self.assertEqual(RecordedThread.started, [commands.handle_queue_check])

    def test_remove_with_a_name_removes_that_file(self):
        self.ctcp("REMOVE some.flac", to=CHANNEL)
        self.assertEqual(RecordedThread.started, [commands.handle_queue_remove_file])


class ThePrivmsgHandsItOver(CTCP):

    def setUp(self):
        super().setUp()
        self.handed = []
        real = irc.answer_ctcp
        irc.answer_ctcp = lambda line, msg, user, target_chan, sock, oserve: self.handed.append(
            (msg, user, target_chan, oserve))
        self.addCleanup(setattr, irc, "answer_ctcp", real)

    def privmsg(self, text, to=PRIVATE):
        line = ":bravo!b@host.example PRIVMSG %s :%s" % (to, text)
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_privmsg(line, irc.parse_privmsg(line), object(), self.oserve)

    def test_a_ctcp_line_is_handed_over_with_its_parts(self):
        self.privmsg("\x01VERSION\x01")
        self.assertEqual(self.handed, [("\x01VERSION\x01", "bravo", PRIVATE, self.oserve)])

    def test_an_ordinary_line_is_not(self):
        self.privmsg("hello there", to=CHANNEL)
        self.assertEqual(self.handed, [])


if __name__ == "__main__":
    unittest.main()
