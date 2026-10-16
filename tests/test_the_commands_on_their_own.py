"""#1342: irc.answer_command(), the command chain of the PRIVMSG dispatch.

It was the elif chain behind answer_privmsg()'s CTCP arm; step 8 of
splitting irc_loop() made it a function. Here it is handed one message and
what it started is read back; the last class checks that answer_privmsg()
hands every non-CTCP line over, with its parts, and no CTCP line.

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
import commands  # noqa: E402
import irc  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase, install_fake_oserve, silence_debug  # noqa: E402

CHANNEL = "#somechannel"
HOST = "b@host.example"


class RecordedThread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target = target

    def start(self):
        RecordedThread.started.append(self.target)


class Commands(DCCoreTestCase):

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

    def command(self, msg, to=CHANNEL):
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_command(msg, msg.lower(), "bravo", HOST, to, irc.get_bot_aliases(), object(), self.oserve)


class TheChain(Commands):

    def test_help_starts_the_help_reply(self):
        self.command("@alfa-help")
        self.assertEqual(RecordedThread.started, [commands.handle_help_request])

    def test_que_starts_the_queue_check(self):
        self.command("@alfa-que")
        self.assertEqual(RecordedThread.started, [commands.handle_queue_check])

    def test_bang_list_explains_how_to_get_the_list(self):
        told = []
        real = list_mod.send_list_trigger_info
        list_mod.send_list_trigger_info = lambda sock, user: told.append(user)
        self.addCleanup(setattr, list_mod, "send_list_trigger_info", real)

        self.command("!list")

        self.assertEqual(told, ["bravo"])

    def test_ordinary_chat_starts_nothing(self):
        self.command("hello there")
        self.assertEqual(RecordedThread.started, [])


class ThePrivmsgHandsItOver(Commands):

    def setUp(self):
        super().setUp()
        self.handed = []
        real = irc.answer_command
        irc.answer_command = lambda msg, msg_lower, user, user_host, target_chan, bot_aliases, sock, oserve: \
            self.handed.append((msg, msg_lower, user, user_host, target_chan, oserve))
        self.addCleanup(setattr, irc, "answer_command", real)

    def privmsg(self, text, to=CHANNEL):
        line = ":bravo!%s PRIVMSG %s :%s" % (HOST, to, text)
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_privmsg(line, irc.parse_privmsg(line), object(), self.oserve)

    def test_a_command_is_handed_over_with_its_parts(self):
        self.privmsg("@alfa-Help")
        self.assertEqual(self.handed, [("@alfa-Help", "@alfa-help", "bravo", HOST, CHANNEL, self.oserve)])

    def test_a_ctcp_line_is_not(self):
        self.privmsg("\x01VERSION\x01", to="alfa")
        self.assertEqual(self.handed, [])


if __name__ == "__main__":
    unittest.main()
