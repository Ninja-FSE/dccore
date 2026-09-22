"""A kicked user stayed "present" until the bot reconnected (audit M27, #629).

The KICK handler only ever looked at a kick of the bot itself. Anyone else
kicked was left in config.channel_users - the map dcc.py reads as proof a
user is present before it dispatches a DCC SEND, and for deciding whether to
freeze a queue. And because the bot then shared no channel with them, their
QUIT was invisible too. A kicked user who disconnected kept being dispatched
to: each attempt held a slot for the accept timeout, the queue was never
frozen and never reaped, and the files were finally discarded as send
failures with a notice to a nick that was not there.

The bot's own kick had the same shape: note_kicked_from() recorded the kick
but left the member list alone, and the rejoin's NAMES only merged into it,
so anyone who left while the bot was out stayed "present" for the life of
the connection.

WHAT THE FIX IS NOT. A kick of the bot from a channel it will rejoin does
NOT drop the member list. Nobody in it did anything, and an absent user's
queue is erased after five minutes - while the rejoin waits for the next
advert, deliberately, so as not to read as a fight with whoever kicked us.
The list is replaced by the first NAMES line after the rejoin instead.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

OURS = "#somechannel"
NOT_OURS = "#somewhere-else"


class SomebodyElseIsKicked(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=OURS)
        self.config.channel_users[OURS] = {"someop", "somebody", "another"}

    def test_they_leave_channel_users(self):
        self.assertTrue(irc.note_user_kicked("Somebody", OURS))

        self.assertEqual(self.config.channel_users[OURS], {"someop", "another"})

    def test_which_is_what_dcc_reads_as_presence(self):
        import dcc

        irc.note_user_kicked("somebody", OURS)

        self.assertFalse(dcc.user_is_present_in_ram("somebody"))
        self.assertTrue(dcc.user_is_present_in_ram("another"))

    def test_the_departure_is_recorded_like_a_part(self):
        """So a reconnect under an alt nick a moment later is recognised
        (#376) - the same observation a PART makes."""
        irc.note_user_kicked("somebody", OURS)

        self.assertEqual(runtime.recent_departures["somebody"]["channel"], OURS)

    def test_the_case_of_the_channel_does_not_matter(self):
        irc.note_user_kicked("somebody", OURS.upper())

        self.assertNotIn("somebody", self.config.channel_users[OURS])

    def test_a_kick_of_someone_we_never_saw_records_nothing(self):
        """Absence alone cannot tell "just left" from "was never here", and a
        false departure merges two strangers' sidebar rows."""
        self.assertFalse(irc.note_user_kicked("stranger", OURS))
        self.assertFalse(irc.note_user_kicked("somebody", "#never-joined"))

        self.assertEqual(runtime.recent_departures, {})
        self.assertEqual(self.config.channel_users[OURS], {"someop", "somebody", "another"})
        self.assertNotIn("#never-joined", self.config.channel_users)


class TheBotItselfIsKicked(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=OURS, REJOIN_ATTEMPTS=3)
        self.config.channel_users[OURS] = {"someop", "somebody", "another"}
        self.config.channel_users[NOT_OURS] = {"someop", "guest"}

    def test_a_channel_it_will_rejoin_keeps_its_members_for_now(self):
        """Dropping them would start the five-minute freeze timer on every
        queue in the channel while the bot waits for the next advert."""
        irc.note_kicked_from(OURS, "someop")

        self.assertEqual(self.config.channel_users[OURS], {"someop", "somebody", "another"})

    def test_but_the_first_names_after_the_rejoin_replaces_them(self):
        irc.note_kicked_from(OURS, "someop")
        # "somebody" left while we were out; we saw neither PART nor QUIT.
        irc.learn_channel_names(OURS, ["someop", "another", "somebot"])

        self.assertEqual(self.config.channel_users[OURS], {"someop", "another", "somebot"})

    def test_and_the_lines_after_the_first_merge_as_before(self):
        """A large channel's NAMES arrives as several 353 lines. Only the
        first one after the kick replaces; the rest must add to it, or a
        big channel would end up holding just its last line."""
        irc.note_kicked_from(OURS, "someop")
        irc.learn_channel_names(OURS, ["someop", "another"])
        irc.learn_channel_names(OURS, ["somebot", "latecomer"])

        self.assertEqual(self.config.channel_users[OURS],
                         {"someop", "another", "somebot", "latecomer"})

    def test_a_later_names_without_a_kick_merges_as_it_always_did(self):
        irc.note_kicked_from(OURS, "someop")
        irc.learn_channel_names(OURS, ["someop"])
        irc.note_joined(OURS)
        irc.learn_channel_names(OURS, ["somebot"])

        self.assertEqual(self.config.channel_users[OURS], {"someop", "somebot"})

    def test_a_channel_it_is_not_going_back_to_is_forgotten(self):
        """Nothing will ever correct that list, and dcc.py reads it as
        presence."""
        self.assertFalse(irc.note_kicked_from(NOT_OURS, "someop"))
        irc.forget_channel_members(NOT_OURS)

        self.assertNotIn(NOT_OURS, self.config.channel_users)
        self.assertEqual(self.config.channel_users[OURS], {"someop", "somebody", "another"})

    def test_forgetting_a_channel_we_never_had_is_harmless(self):
        irc.forget_channel_members("#never-joined")

    def test_a_names_line_for_a_new_channel_still_creates_it(self):
        irc.learn_channel_names("#newchannel", ["someop", "somebody"])

        self.assertEqual(self.config.channel_users["#newchannel"], {"someop", "somebody"})


class TheReadLoopIsWired(unittest.TestCase):
    """The handlers live inside irc_loop(), a single function reading a live
    socket that this suite does not run line-by-line - so, as
    test_rejoin_a_channel_that_kicked_us.py does for the same branch, the
    wiring is checked in the text. Every test above would pass with the
    helpers never called."""

    def source(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def kick_branch(self):
        return self.source().split("kick_parsed = parse_kick(line)", 1)[1][:1500]

    def test_a_kick_of_somebody_else_is_mirrored(self):
        self.assertIn("note_user_kicked(victim, kicked_chan)", self.kick_branch())

    def test_the_channel_we_are_not_going_back_to_is_forgotten(self):
        self.assertIn("forget_channel_members(kicked_chan)", self.kick_branch())

    def test_the_names_reply_goes_through_the_helper(self):
        names_branch = self.source().split('is_server_numeric(line, "353")', 1)[1][:1500]

        self.assertIn("learn_channel_names(chan, names)", names_branch)
        self.assertNotIn(".update(names)", names_branch,
                         "the 353 handler is merging on its own again")


if __name__ == "__main__":
    unittest.main()
