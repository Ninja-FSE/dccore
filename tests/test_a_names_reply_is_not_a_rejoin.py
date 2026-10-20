"""A NAMES reply for a channel the bot is not in is not a rejoin.

The rehash's channel sync asks NAMES of every CHANNEL, one the bot was kicked
from or banned out of included, and a server answers NAMES for a channel the
client is not on as well: 366 always, 353 too when the channel is public.
answer_any_end_of_names() took any 366 as proof the bot was in, so one Save
on the dashboard cleared the kick (or the give-up, or #1415's join wait),
said "Rejoined", stopped every attempt to get back in, and sent adverts to a
channel the bot was banned from; answer_names() filled a member list the bot
would never see a PART or QUIT from.

The server's echo of the bot's own JOIN is the proof (irc.note_own_join()):
a channel the bot knows it is out of counts as joined again only after it.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402

from tests import test_the_reconnect_backs_off as reconnect  # noqa: E402
from tests.support import DCCoreTestCase, RecordingSocket, install_fake_oserve, silence_debug  # noqa: E402

NAMES = ":irc.example.net 353 SomeBot = {} :alice bob carol"
END = ":irc.example.net 366 SomeBot {} :End of /NAMES list."


class Harness(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#alpha,#beta", DEBUG_CHANNEL="", NICKNAME="SomeBot",
                        REJOIN_ATTEMPTS=3, activation_triggered=True)
        with runtime.channel_users_lock():
            config.channel_users["#alpha"] = {"somebot", "dave"}
            config.channel_users["#beta"] = {"somebot", "erin"}
        self.debug = silence_debug(announce)
        self.sock = RecordingSocket()
        self.conn = irc.Connection(self.sock, config.connection_epoch)
        self.conn.joined = True
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def names_reply(self, channel):
        """What the server sends back for NAMES <channel>."""
        irc.answer_names(NAMES.format(channel), self.sock)
        return irc.answer_any_end_of_names(END.format(channel), self.conn, self.sock, self.conn.epoch)

    def rejoined_said(self):
        return [text for _category, text in self.debug if text.startswith("Rejoined")]


class TheRehashAsksNamesOfAChannelTheBotIsOutOf(Harness):

    def test_the_sync_asks_it(self):
        """The trigger, so the tests below are about a line that really comes."""
        irc.note_kicked_from("#beta", "someop")
        chans = commands._channels_to_sync(config)
        lines = commands.sync_channels(install_fake_oserve(), chans, chans, log=lambda *_: None)

        self.assertTrue(any(line.startswith("NAMES ") and "#beta" in line for line in lines))

    def test_a_kicked_channel_stays_out(self):
        irc.note_kicked_from("#beta", "someop")

        self.assertIsNone(self.names_reply("#beta"))

        self.assertIn("#beta", config.kicked_channels)
        self.assertEqual(irc.channels_to_rejoin(), ["#beta"])
        self.assertIn("#beta", irc.channels_we_are_out_of())
        self.assertNotIn("#beta", self.conn.channels_confirmed)
        self.assertEqual(self.rejoined_said(), [])
        self.assertFalse(irc._in_channel("#beta"))

    def test_a_given_up_channel_stays_given_up(self):
        irc.note_kicked_from("#beta", "someop")
        for _ in range(3):
            irc.note_join_refused("#beta")
        self.assertEqual(irc.gave_up_on(), ["#beta"])

        self.names_reply("#beta")

        self.assertEqual(irc.gave_up_on(), ["#beta"])
        self.assertIn("#beta", irc.channels_we_are_out_of())
        self.assertEqual(self.rejoined_said(), [])

    def test_its_members_are_not_learned(self):
        """Nobody in a channel the bot is out of is 'present' to dcc.py: their
        PART and QUIT would never be seen."""
        irc.note_kicked_from("#beta", "someop")
        for _ in range(3):
            irc.note_join_refused("#beta")   # given up: its list is dropped

        self.names_reply("#beta")

        with runtime.channel_users_lock():
            self.assertNotIn("#beta", config.channel_users)

    def test_a_join_wait_stays(self):
        """#1415: a 439 for the JOIN; the NAMES reply does not end the wait."""
        irc.note_join_unconfirmed("#beta")
        with runtime.channel_users_lock():
            config.channel_users.pop("#beta", None)
        self.assertIsNotNone(irc.note_join_target_wait("#beta", 100))

        self.names_reply("#beta")

        self.assertIn("#beta", runtime.join_waits)
        self.assertIn("#beta", config.kicked_channels)

    def test_a_join_wait_on_its_own_stays(self):
        """A channel can wait with no retry entry: a 439 to the connect-time
        JOIN, or one defer_joins_after_a_refusal() held back. The wait alone
        marks it as out."""
        with runtime.channel_users_lock():
            config.channel_users.pop("#beta", None)
        self.assertIsNotNone(irc.note_join_target_wait("#beta", 100))
        self.assertNotIn("#beta", config.kicked_channels)

        self.assertIsNone(self.names_reply("#beta"))

        self.assertIn("#beta", runtime.join_waits)
        self.assertNotIn("#beta", self.conn.channels_confirmed)
        with runtime.channel_users_lock():
            self.assertNotIn("#beta", config.channel_users)

    def test_before_activation_it_does_not_count_either(self):
        self.set_config(activation_triggered=False)
        self.conn.target_channels = {"#alpha", "#beta"}
        irc.note_kicked_from("#beta", "someop")

        irc.answer_end_of_names(END.format("#beta"), self.conn)

        self.assertNotIn("#beta", self.conn.channels_confirmed)

    def test_a_channel_the_bot_is_in_is_confirmed_as_before(self):
        """Nothing changes for a channel nobody marked as out."""
        self.assertEqual(self.names_reply("#alpha"), "#alpha")
        self.assertIn("#alpha", self.conn.channels_confirmed)
        with runtime.channel_users_lock():
            self.assertEqual(config.channel_users["#alpha"], {"somebot", "dave", "alice", "bob", "carol"})


class TheBotsOwnJoinIsTheProof(Harness):

    def test_a_real_rejoin_is_cleared_and_said(self):
        irc.note_kicked_from("#beta", "someop")

        irc.note_own_join("#Beta")
        self.assertEqual(self.names_reply("#beta"), "#beta")

        self.assertNotIn("#beta", config.kicked_channels)
        self.assertIn("#beta", self.conn.channels_confirmed)
        self.assertEqual(self.rejoined_said(), ["Rejoined #beta."])
        # The first list after the kick replaces the old one (members_stale).
        with runtime.channel_users_lock():
            self.assertEqual(config.channel_users["#beta"], {"alice", "bob", "carol"})

    def test_a_join_wait_ends_with_the_join(self):
        irc.note_join_unconfirmed("#beta")
        irc.note_join_target_wait("#beta", 100)

        irc.note_own_join("#beta")
        self.names_reply("#beta")

        self.assertEqual(runtime.join_waits, {})
        self.assertNotIn("#beta", config.kicked_channels)

    def test_a_late_member_list_still_confirms_a_channel_the_bot_joined(self):
        """#1424's case: the JOIN was echoed, its 366 came after the watchdog
        had called the channel never confirmed."""
        irc.note_own_join("#beta")
        irc.note_join_unconfirmed("#beta")

        self.assertEqual(self.names_reply("#beta"), "#beta")

        self.assertNotIn("#beta", config.kicked_channels)

    def test_a_kick_after_the_join_takes_it_back(self):
        irc.note_own_join("#beta")
        irc.note_kicked_from("#beta", "someop")

        self.names_reply("#beta")

        self.assertIn("#beta", config.kicked_channels)

    def test_a_kick_from_a_channel_not_ours_takes_it_back_too(self):
        """note_kicked_from() returns early for a channel not in CHANNEL, but
        the echo must still go: kept, it would vouch for the channel once the
        operator adds it to CHANNEL, and a NAMES reply would rejoin it."""
        irc.note_own_join("#gamma")

        self.assertFalse(irc.note_kicked_from("#gamma", "someop"))

        self.assertNotIn("#gamma", runtime.joined_channels)
        self.set_config(CHANNEL="#alpha,#beta,#gamma")
        irc.note_join_unconfirmed("#gamma")
        self.assertIsNone(self.names_reply("#gamma"))
        self.assertIn("#gamma", config.kicked_channels)

    def test_a_part_by_the_bot_takes_it_back(self):
        irc.note_own_join("#beta")
        irc.answer_part(":SomeBot!u@h.example PART #beta :bye")
        irc.note_join_unconfirmed("#beta")

        self.names_reply("#beta")

        self.assertIn("#beta", config.kicked_channels)

    def test_someone_elses_part_does_not(self):
        irc.note_own_join("#beta")
        irc.answer_part(":erin!u@h.example PART #beta :bye")
        irc.note_join_unconfirmed("#beta")

        self.names_reply("#beta")

        self.assertNotIn("#beta", config.kicked_channels)

    def test_a_new_connection_forgets_the_old_ones_joins(self):
        irc.note_own_join("#beta")

        irc.forget_join_waits()

        self.assertEqual(runtime.joined_channels, set())


class TheReadLoopPassesTheEchoOn(reconnect.ASeriesOfConnections):
    """irc.irc_loop() against a scripted server (#663's harness): the lines
    on the wire reach note_own_join() and note_own_leave()."""

    OWN_JOIN = ":SomeBot!u@h.example JOIN :#Beta"

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#alpha,#beta", DEBUG_CHANNEL="")

    def test_the_bots_own_join_is_recorded_and_no_one_elses(self):
        self.run_connections([reconnect.WELCOME, self.OWN_JOIN, ":erin!u@h.example JOIN #alpha"])

        self.assertEqual(runtime.joined_channels, {"#beta"})

    def test_a_kick_on_the_wire_takes_it_back(self):
        self.run_connections([reconnect.WELCOME, self.OWN_JOIN,
                              ":someop!u@h.example KICK #beta SomeBot :out"])

        self.assertEqual(runtime.joined_channels, set())
        self.assertIn("#beta", config.kicked_channels)

    def test_a_new_connection_starts_with_none(self):
        self.run_connections([reconnect.WELCOME, self.OWN_JOIN], [reconnect.WELCOME])

        self.assertEqual(runtime.joined_channels, set())


for _name in [n for n in dir(reconnect.ASeriesOfConnections) if n.startswith("test")]:
    setattr(TheReadLoopPassesTheEchoOn, _name, None)


if __name__ == "__main__":
    unittest.main()
