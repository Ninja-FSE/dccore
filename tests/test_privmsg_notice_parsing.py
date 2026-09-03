"""irc.parse_privmsg()/parse_notice() - #162 finding #5.

Companion to test_membership_events.py (JOIN/PART/QUIT/NICK) and
test_server_numerics.py (the numerics) - PRIVMSG and NOTICE were the last
two dispatch patterns in irc.py never anchored, with two independent
problems:

(a) A greedy `.*` between the prefix and the command word latches onto the
    LAST " PRIVMSG <target> :" (or NOTICE) in the line, which can sit
    inside the message BODY. A private message whose text contained
    " PRIVMSG SomeVictim :!DCCore <filename>" parsed target_chan as
    "SomeVictim" - a real, completed transfer whose "Sent:" advert then
    went to an arbitrary channel or nick nobody asked for.

(b) The nick group `[^!]+` matched across whitespace, so a server numeric's
    prefix (which has no "!" but does have spaces) ran straight into the
    numeric's own text. `:irc.undernet.org 332 DCCore #chan :Welcome!
    PRIVMSG #chan :!DCCore Song.flac` parsed as
    user='irc.undernet.org 332 DCCore #chan :Welcome', used verbatim as a
    NOTICE target and a persisted dcc_queue key. Topic 332 fires on every
    JOIN and every topic change.

The conditions under test are READ OUT OF irc.py and evaluated, not
retyped, for the same reason test_membership_events.py already gives: a
test that only searches source text agrees with itself, not with the
daemon.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402


class GenuinePrivmsgAndNoticeStillParse(unittest.TestCase):

    def test_a_channel_privmsg_parses_correctly(self):
        line = ":dave!~u@host.example.com PRIVMSG #dccore-test :!DCCore Song.flac"
        parsed = irc.parse_privmsg(line)
        self.assertIsNotNone(parsed)
        nick, ident_host, target, message = parsed
        self.assertEqual(nick, "dave")
        self.assertEqual(ident_host, "~u@host.example.com")
        self.assertEqual(target, "#dccore-test")
        self.assertEqual(message, "!DCCore Song.flac")

    def test_a_private_privmsg_to_the_bot_parses_correctly(self):
        line = ":dave!~u@host.example.com PRIVMSG DCCore :@DCCore-help"
        nick, ident_host, target, message = irc.parse_privmsg(line)
        self.assertEqual(target, "DCCore")
        self.assertEqual(message, "@DCCore-help")

    def test_an_admin_style_hostmask_parses(self):
        line = ":SysOp!sysop@Undernet.CoolGuy.Users PRIVMSG DCCore :!rehash"
        nick, ident_host, target, message = irc.parse_privmsg(line)
        self.assertEqual(nick, "SysOp")
        self.assertEqual(ident_host, "sysop@Undernet.CoolGuy.Users")

    def test_a_channel_notice_parses_correctly(self):
        line = ":goodbot!u@h NOTICE #dccore-test :Search results for metallica"
        nick, target, message = irc.parse_notice(line)
        self.assertEqual(nick, "goodbot")
        self.assertEqual(target, "#dccore-test")
        self.assertEqual(message, "Search results for metallica")

    def test_a_private_notice_parses_correctly(self):
        line = ":otherbot!u@h NOTICE DCCore :Rar Server is currently disabled."
        nick, target, message = irc.parse_notice(line)
        self.assertEqual(target, "DCCore")


class MessageBodyCannotForgeASecondCommand(unittest.TestCase):
    """The greedy `.*` vulnerability (5a) - a genuinely-completed repro."""

    def test_a_privmsg_body_containing_privmsg_does_not_redirect_the_target(self):
        """The exact repro shape: a private message to the bot whose own
        text contains a second, fake ' PRIVMSG <target> :' - the REAL
        target (the one this line was actually sent to) must win, and the
        fake one must land in the message body as inert text."""
        line = (":attacker!u@h PRIVMSG DCCore :"
                "!DCCore Song.flac PRIVMSG SomeVictim :fake advert body")
        nick, ident_host, target, message = irc.parse_privmsg(line)
        self.assertEqual(target, "DCCore")
        self.assertIn("PRIVMSG SomeVictim", message,
                     "the fake command must be inert text, not re-parsed")

    def test_a_notice_body_containing_notice_does_not_redirect_the_target(self):
        line = (":attacker!u@h NOTICE DCCore :"
                "hello NOTICE SomeVictim :fake text")
        nick, target, message = irc.parse_notice(line)
        self.assertEqual(target, "DCCore")

    def test_a_search_result_mentioning_privmsg_is_not_misparsed(self):
        """Not just an attack - an ordinary filename or search term
        containing the literal word could trip the old greedy regex too."""
        line = ":dave!u@h PRIVMSG #dccore-test :@find PRIVMSG SomeOne handler.mp3"
        nick, ident_host, target, message = irc.parse_privmsg(line)
        self.assertEqual(target, "#dccore-test")
        self.assertEqual(message, "@find PRIVMSG SomeOne handler.mp3")


class ServerNumericsCannotForgeAUserPrefix(unittest.TestCase):
    """The unanchored-nick vulnerability (5b)."""

    def test_a_topic_numeric_is_not_parsed_as_a_privmsg(self):
        """The audit's own reproduction verbatim."""
        line = (":irc.undernet.org 332 DCCore #chan :Welcome! "
                "PRIVMSG #chan :!DCCore Song.flac")
        self.assertIsNone(irc.parse_privmsg(line))

    def test_a_topic_numeric_is_not_parsed_as_a_notice(self):
        line = (":irc.undernet.org 332 DCCore #chan :Welcome! "
                "NOTICE #chan :something")
        self.assertIsNone(irc.parse_notice(line))

    def test_the_motd_is_not_parsed_as_a_privmsg(self):
        line = ":irc.undernet.org 372 DCCore :the motd mentions PRIVMSG here"
        self.assertIsNone(irc.parse_privmsg(line))

    def test_a_server_sourced_line_has_no_ident_host_to_forge(self):
        """A server prefix has no "!" at all - group 1 must stop and refuse
        to match rather than swallowing the whole prefix as a "nick"."""
        line = ":irc.undernet.org NOTICE DCCore :server notice"
        self.assertIsNone(irc.parse_notice(line))


class MalformedLinesAreRefused(unittest.TestCase):

    def test_a_prefixless_line_is_refused(self):
        self.assertIsNone(irc.parse_privmsg("PRIVMSG #dccore-test :hello"))
        self.assertIsNone(irc.parse_notice("NOTICE #dccore-test :hello"))

    def test_a_privmsg_with_no_message_text_is_refused(self):
        """No ':' separator at all - not even an empty message."""
        self.assertIsNone(irc.parse_privmsg(":dave!u@h PRIVMSG #dccore-test"))

    def test_a_command_must_not_merely_start_the_word(self):
        """PRIVMSGX is not PRIVMSG - \\s+ is what stops the prefix match."""
        line = ":dave!u@h PRIVMSGX #dccore-test :hello"
        self.assertIsNone(irc.parse_privmsg(line))


if __name__ == "__main__":
    unittest.main()
