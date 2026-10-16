"""#1332: the end of NAMES, KICK and JOIN-refusal handlers, called directly.

Branches of irc_loop() until step 5 of splitting it; the loop-driving tests
(test_audit_irc_connection.py, test_a_join_nobody_checked.py and others)
still cover the wiring. Each is called here with one line, or what was
parsed from it, and its effect read back.

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
from tests.test_the_bots_own_nick_follows_the_server import (  # noqa: E402
    NOTICE_AUTH, DrivesPastRegistration, welcome)

SERVER = ":irc.example.net"


class RecordedThread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, tuple(args)

    def start(self):
        RecordedThread.started.append((self.target, self.args))


class Handlers(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="alfa", ORIGINAL_NICK="alfa", CHANNEL="#one,#two", DEBUG_CHANNEL="",
                        activation_triggered=False)
        config.channel_users.clear()
        silence_debug(announce)
        RecordedThread.started = []
        stub = types.ModuleType("threading")
        stub.__dict__.update(threading.__dict__)
        stub.Thread = RecordedThread
        real = irc.threading
        irc.threading = stub
        self.addCleanup(setattr, irc, "threading", real)
        self.conn = irc.Connection(object(), 1)
        self.conn.joined = True
        self.conn.target_channels = {"#one", "#two"}

    def quietly(self, call, *args):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            call(*args)
        return said.getvalue()


class TheEndOfNames(Handlers):

    def end(self, chan):
        return self.quietly(irc.answer_end_of_names, SERVER + " 366 alfa %s :End of /NAMES list." % chan, self.conn)

    def test_one_channel_of_two_is_confirmed_and_does_not_activate(self):
        said = self.end("#one")

        self.assertEqual(self.conn.channels_confirmed, {"#one"})
        self.assertIn("(1/2 target channels confirmed)", said)
        self.assertFalse(config.activation_triggered)
        self.assertEqual(RecordedThread.started, [])

    def test_the_last_one_activates_this_connection(self):
        self.end("#one")
        said = self.end("#two")

        self.assertTrue(config.activation_triggered)
        self.assertIn("All channels joined successfully", said)
        self.assertIn((irc.delayed_activate, (self.conn,)),
                      RecordedThread.started)

    def test_it_reads_the_target_channels_from_the_connection(self):
        self.conn.target_channels = {"#one"}
        self.end("#one")
        self.assertTrue(config.activation_triggered)


class TheLoopWaitsForEveryChannel(DrivesPastRegistration):
    """The real read loop with two channels: one channel's end of NAMES must
    not activate. The connection carries the channel list the 366 handler
    compares against (#1332); left at its empty default, the first 366 would
    activate."""

    def test_the_end_of_names_is_handed_every_target_channel(self):
        # Read when the loop calls the handler: the loop's disconnect step
        # resets activation_triggered before a test could look at it.
        self.set_config(CHANNEL="#one,#two", DEBUG_CHANNEL="", activation_triggered=False)
        seen = []
        real = irc.answer_end_of_names

        def spy(line, conn):
            seen.append(set(conn.target_channels))
            return real(line, conn)
        irc.answer_end_of_names = spy
        self.addCleanup(setattr, irc, "answer_end_of_names", real)

        with contextlib.redirect_stdout(io.StringIO()):
            self.run_registration(NOTICE_AUTH, welcome("SomeBot"),
                                  SERVER + " 366 SomeBot #one :End of /NAMES list.")

        self.assertEqual(seen, [{"#one", "#two"}])


class TheKick(Handlers):

    def test_a_kick_of_somebody_else_is_their_departure(self):
        config.channel_users["#one"] = {"alfa", "bravo"}
        self.quietly(irc.answer_kick, ("someop", "#one", "bravo"))
        self.assertEqual(sorted(config.channel_users["#one"]), ["alfa"])

    def test_a_kick_of_the_bot_is_remembered_for_a_rejoin(self):
        self.quietly(irc.answer_kick, ("someop", "#one", "alfa"))
        self.assertEqual(config.kicked_channels["#one"]["by"], "someop")

    def test_a_kick_from_a_channel_it_does_not_serve_forgets_the_members(self):
        config.channel_users["#elsewhere"] = {"alfa", "bravo"}
        said = self.quietly(irc.answer_kick, ("someop", "#elsewhere", "alfa"))
        self.assertNotIn("#elsewhere", config.channel_users)
        self.assertIn("not rejoining", said)


class TheJoinRefusal(Handlers):

    def test_a_refusal_counts_against_a_channel_being_rejoined(self):
        irc.note_kicked_from("#one", "someop")
        said = self.quietly(irc.answer_join_refusal, ("#one", "474"))
        self.assertEqual(config.kicked_channels["#one"]["refusals"], 1)
        self.assertIn("#one", said)


if __name__ == "__main__":
    unittest.main()
