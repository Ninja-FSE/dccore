"""What the server calls us, taken from the server rather than assumed.

WHAT WENT WRONG

A nickname longer than the server's NICKLEN is not refused - it is silently
SHORTENED. Undernet allows 12, so a bot configured as "DCCore-Server"
registered as "DCCore-Serve" and the daemon never noticed: it logged
"CURRENT_NICK settled as: DCCore-Server", advertised "@DCCore-Server" - a nick
nobody could PM or DCC - and answered to a name it did not have.

433 (nick already in use) was handled, because that one announces itself.
Truncation does not. Nothing failed, so nothing was caught, and it took an
operator noticing their bot in the channel was called something else.

WHY THE TARGET FIELD

Every numeric reply is ":<prefix> <code> <target> ..." and for a registered
client <target> IS its nick. That is RFC 1459/2812 message structure rather
than a courtesy, so every IRCd does it. The TEXT of RPL_WELCOME is whatever a
network chose to write; the target position is fixed. So this reads the field
and never the sentence.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheNickComesFromTheNumericsTarget(unittest.TestCase):

    def test_it_reads_the_field_not_the_welcome_text(self):
        """Real 001 lines from three networks. The wording differs on every
        one of them; the target position does not."""
        cases = [
            (":irc.undernet.org 001 DCCore-Serve "
             ":Welcome to the Undernet IRC Network", "DCCore-Serve"),
            (":hitchcock.freenode.net 001 MyBot "
             ":Welcome to the freenode Internet Relay Chat Network MyBot", "MyBot"),
            (":irc.local 001 Short :Welcome", "Short"),
        ]
        for line, expected in cases:
            with self.subTest(line=line[:40]):
                self.assertEqual(irc.numeric_target(line), expected)

    def test_a_truncated_nick_is_reported_as_the_server_gave_it(self):
        """The actual defect: 13 characters configured, 12 registered."""
        line = ":irc.undernet.org 001 DCCore-Serve :Welcome"

        self.assertEqual(irc.numeric_target(line), "DCCore-Serve")
        self.assertNotEqual(irc.numeric_target(line), "DCCore-Server")

    def test_the_unregistered_star_is_not_a_nick(self):
        """Before registration completes a server addresses an unknown client
        as "*". Adopting that would rename the bot to an asterisk."""
        self.assertIsNone(irc.numeric_target(":server 020 * :Please wait"))

    def test_a_forged_numeric_in_channel_text_is_ignored(self):
        """The reason is_server_numeric() exists at all: the read loop sees
        every PRIVMSG as raw text first, and "001" once matched an ordinary
        music request. An unanchored version of this would let anyone in a
        channel rename the bot by typing at it."""
        forged = ":nick!user@host PRIVMSG #chan :look 001 EvilNick :hello"

        self.assertIsNone(irc.numeric_target(forged))

    def test_a_line_that_is_not_a_numeric_at_all(self):
        for line in ("PING :12345", "", ":server NOTICE * :hi", "garbage"):
            with self.subTest(line=line):
                self.assertIsNone(irc.numeric_target(line))


class TheServerStatesItsNickLimit(unittest.TestCase):
    """005 RPL_ISUPPORT is where the limit is published. Reading it is what
    turns "the bot is called something else" into a sentence saying why."""

    def test_it_reads_nicklen(self):
        line = (":irc.undernet.org 005 Bot WHOX WALLCHOPS NICKLEN=12 "
                "MAXNICKLEN=15 :are supported by this server")

        self.assertEqual(irc.isupport_nicklen(line), 12)

    def test_it_is_not_fooled_by_the_trailing_prose(self):
        """Everything after " :" is human text a network writes freely. A
        value found there is not a limit."""
        line = ":x 005 Bot CHANTYPES=# :NICKLEN=99 is mentioned in the text"

        self.assertIsNone(irc.isupport_nicklen(line))

    def test_it_does_not_match_a_longer_token(self):
        """MAXNICKLEN= contains NICKLEN=, and they mean different things."""
        line = ":x 005 Bot MAXNICKLEN=32 :are supported by this server"

        self.assertIsNone(irc.isupport_nicklen(line))

    def test_absent_means_unknown_not_unlimited(self):
        """Not every server sends NICKLEN - it is a convention, not a
        standard - so None has to read as "no information", and the caller
        must not treat it as permission."""
        line = ":x 005 Bot CHANTYPES=# PREFIX=(ov)@+ :are supported"

        self.assertIsNone(irc.isupport_nicklen(line))

    def test_only_a_real_005_counts(self):
        self.assertIsNone(irc.isupport_nicklen(":x 001 Bot :NICKLEN=12"))
        self.assertIsNone(irc.isupport_nicklen(
            ":n!u@h PRIVMSG #c :005 Bot NICKLEN=1 :spoofed"))


class AdoptingTheServersNickKeepsTheListWorking(DCCoreTestCase):
    """The reason the shortened name goes into NICKLEN and not over
    ORIGINAL_NICK.

    The master list is built by update_list.py as a SUBPROCESS, which imports
    a fresh config - so its request lines carry the CONFIGURED name, not the
    shortened one. If adopting the server's nick lost the configured one, every
    "!<nick> <file>" pasted out of the list would stop matching, which is the
    exact failure get_bot_aliases() was written for after a 433.
    """

    def test_the_bot_answers_to_both_names(self):
        self.set_config(NICKNAME="DCCore-Serve",        # what the server gave
                        ORIGINAL_NICK="DCCore-Server")  # what the list carries

        aliases = irc.get_bot_aliases()

        self.assertIn("dccore-serve", aliases)
        self.assertIn("dccore-server", aliases)

    def test_the_live_nick_comes_first(self):
        """get_bot_aliases() promises current nick first, and the advert
        publishes the live one - which after this fix is a nick that exists."""
        self.set_config(NICKNAME="DCCore-Serve", ORIGINAL_NICK="DCCore-Server")

        self.assertEqual(irc.get_bot_aliases()[0], "dccore-serve")

    def test_nothing_changes_when_the_server_agrees(self):
        """Control. The overwhelmingly common case is a nickname that fits, and
        it must collapse to exactly one alias as it always did."""
        self.set_config(NICKNAME="DCCore", ORIGINAL_NICK="DCCore")

        self.assertEqual(irc.get_bot_aliases(), ["dccore"])


if __name__ == "__main__":
    unittest.main()
