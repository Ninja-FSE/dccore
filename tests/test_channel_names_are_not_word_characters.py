"""A channel name is not restricted to word characters.

FOUND IN A BETA CHANNEL, not by a test. An operator added a channel with an
"&" in its name. The bot joined it, sat in it, and advertised into it on
schedule - and answered nothing at all. @<nick>, @find, -help, -que: every one
silently dropped, in that channel only.

Joining and advertising are OUTBOUND. Neither parses a line, which is why
both looked perfectly healthy.

parse_privmsg() matched its target as `[#\\w\\-]+` - "#", letters, digits,
underscore, hyphen. RFC 2812 says a channel is a "#", "&", "+" or "!" prefix
followed by any octet except NUL, BEL, CR, LF, space and comma. "&" is not
exotic; neither is "^", which is just as common in music-channel names. Any
message from such a channel failed to match at all, and a PRIVMSG that does
not parse is a PRIVMSG that never happened.

The same character class cost three more things, all of them silent:

  * the 366 (End of NAMES) confirmation, so the channel never counted as
    joined during startup
  * JOIN tracking, which dcc.py reads as proof of presence before it
    dispatches a send
  * PART tracking, which freezes a leaver's queue

WIDENING ALONE WOULD HAVE BEEN WRONG. `\\S+` on its own lets a hostile server
hand us a "channel" containing \\x01 or a bare \\r, and the target is
interpolated straight back into our own outbound lines. The parsers take
`\\S+` - which is what the protocol means, a space being the field separator -
and validate separately, so every caller inherits the check.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402

# Invented, and deliberately awkward: an ampersand, a caret, and the bracket
# characters RFC 2812 lists as legal in a nick and which turn up in channel
# names too.
AMPERSAND_CHANNEL = "#alpha&beta"
CARET_CHANNEL = "#gamma^delta^music"
BRACKET_CHANNEL = "#epsilon[zeta]"
PLAIN_CHANNEL = "#plainchannel"


class APrivmsgFromSuchAChannelParses(unittest.TestCase):

    def line(self, channel, text="@somebot"):
        return f":someuser!ident@example.invalid PRIVMSG {channel} :{text}"

    def test_an_ampersand_in_the_name(self):
        """The reported case. This returned None, so the bot answered
        nothing at all in that channel."""
        parsed = irc.parse_privmsg(self.line(AMPERSAND_CHANNEL))

        self.assertIsNotNone(parsed, "a message from this channel does not "
                                     "parse, so the bot is mute in it")
        self.assertEqual(parsed[2], AMPERSAND_CHANNEL)

    def test_a_caret_in_the_name(self):
        parsed = irc.parse_privmsg(self.line(CARET_CHANNEL))

        self.assertEqual(parsed[2], CARET_CHANNEL)

    def test_brackets_in_the_name(self):
        parsed = irc.parse_privmsg(self.line(BRACKET_CHANNEL))

        self.assertEqual(parsed[2], BRACKET_CHANNEL)

    def test_the_message_body_survives_intact(self):
        """The target group got wider; the body must not have moved."""
        parsed = irc.parse_privmsg(self.line(AMPERSAND_CHANNEL, "@find one two"))

        self.assertEqual(parsed[3], "@find one two")

    def test_a_body_containing_a_colon_is_still_whole(self):
        parsed = irc.parse_privmsg(
            self.line(AMPERSAND_CHANNEL, "see: this and that"))

        self.assertEqual(parsed[3], "see: this and that")

    def test_an_ordinary_channel_is_unchanged(self):
        parsed = irc.parse_privmsg(self.line(PLAIN_CHANNEL))

        self.assertEqual(
            parsed, ("someuser", "ident@example.invalid", PLAIN_CHANNEL,
                     "@somebot"))

    def test_a_private_message_still_parses(self):
        parsed = irc.parse_privmsg(
            ":someuser!ident@example.invalid PRIVMSG SomeBot :hello")

        self.assertEqual(parsed[2], "SomeBot")


class ANoticeFromSuchAChannelParses(unittest.TestCase):
    """Same class, same fix: this is how another bot's advert reaches us."""

    def test_an_ampersand_in_the_name(self):
        parsed = irc.parse_notice(
            f":otherbot!ident@example.invalid NOTICE {AMPERSAND_CHANNEL} :hi")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1], AMPERSAND_CHANNEL)

    def test_an_ordinary_notice_is_unchanged(self):
        parsed = irc.parse_notice(
            ":otherbot!ident@example.invalid NOTICE SomeBot :hi there")

        self.assertEqual(parsed, ("otherbot", "SomeBot", "hi there"))


class ATargetIsStillValidated(unittest.TestCase):
    """Widening the regex without this would let a hostile server hand us a
    target that we then interpolate into our own outbound lines."""

    def test_a_plain_channel_is_accepted(self):
        self.assertTrue(irc.is_valid_irc_target(PLAIN_CHANNEL))

    def test_the_awkward_but_legal_names_are_accepted(self):
        for channel in (AMPERSAND_CHANNEL, CARET_CHANNEL, BRACKET_CHANNEL):
            self.assertTrue(irc.is_valid_irc_target(channel), channel)

    def test_a_ctcp_delimiter_is_refused(self):
        """A body containing \\x01 is read as an inline CTCP by the receiving
        client - the same rule dcc_fetch.contains_unsafe_ctcp_bytes() applies
        to everything that reaches a raw outbound line."""
        self.assertFalse(irc.is_valid_irc_target("#chan\x01VERSION\x01"))

    def test_a_carriage_return_is_refused(self):
        self.assertFalse(irc.is_valid_irc_target("#chan\rQUIT"))

    def test_a_newline_is_refused(self):
        self.assertFalse(irc.is_valid_irc_target("#chan\nJOIN #elsewhere"))

    def test_a_nul_is_refused(self):
        self.assertFalse(irc.is_valid_irc_target("#chan\x00"))

    def test_a_bel_is_refused(self):
        self.assertFalse(irc.is_valid_irc_target("#chan\x07"))

    def test_a_comma_is_refused(self):
        """A comma separates a target LIST, so a single target containing one
        is not a single target."""
        self.assertFalse(irc.is_valid_irc_target("#chan,#elsewhere"))

    def test_nothing_is_refused(self):
        self.assertFalse(irc.is_valid_irc_target(""))
        self.assertFalse(irc.is_valid_irc_target(None))


class TheParsersRefuseWhatTheValidatorRefuses(unittest.TestCase):
    """The point of validating at parse time: a caller cannot forget."""

    def test_a_privmsg_with_a_ctcp_delimiter_in_its_target_is_dropped(self):
        parsed = irc.parse_privmsg(
            ":someuser!ident@example.invalid PRIVMSG "
            "#chan\x01VERSION\x01 :hello")

        self.assertIsNone(parsed)

    def test_a_notice_with_a_comma_in_its_target_is_dropped(self):
        parsed = irc.parse_notice(
            ":otherbot!ident@example.invalid NOTICE #one,#two :hello")

        self.assertIsNone(parsed)

    def test_a_malformed_line_is_still_none(self):
        self.assertIsNone(irc.parse_privmsg("not an irc line at all"))
        self.assertIsNone(irc.parse_notice(":x!y PRIVMSG #chan :wrong verb"))


class TheLoopParsersTakeTheSameNames(unittest.TestCase):
    """366, JOIN and PART are matched inline in irc_loop() rather than by a
    named function, so they are checked against the source. Each one was the
    same character class and each failed the same way: the 366 meant the
    channel never counted as confirmed at startup, and JOIN/PART meant
    config.channel_users never learned who was in it - which dcc.py reads as
    proof of presence before it dispatches a send."""

    def source(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def code_lines(self):
        """Comments only, stripped - the fix's own comment quotes the old
        pattern to explain it, and a guard that cannot tell prose from code
        would fail on the explanation rather than on a regression."""
        return [line for line in self.source().splitlines()
                if not line.lstrip().startswith("#")]

    def test_no_parser_still_restricts_a_target_to_word_characters(self):
        offenders = [line.strip() for line in self.code_lines()
                     if r"[#\w\-]+" in line]

        self.assertEqual(offenders, [],
                         "a parser still matches its target as word "
                         "characters only - see this file's docstring")

    def test_that_guard_can_actually_see_code(self):
        """Guard on the guard: if code_lines() returned nothing, or dropped
        every line with a regex in it, the check above would pass on a fully
        broken file."""
        code = "\n".join(self.code_lines())

        self.assertIn("def parse_privmsg(line):", code)
        self.assertIn(r"PRIVMSG\s+(\S+)\s+:(.+)$", code)

    def test_the_end_of_names_line_takes_any_target(self):
        source = self.source()

        self.assertIn(r'"^:\S+ 366 \S+ (\S+)"', source)
        self.assertIn("is_valid_irc_target(m366.group(1))", source)

    def test_join_takes_any_target(self):
        source = self.source()

        self.assertIn(r'JOIN :?(\S+)"', source)
        self.assertIn("is_valid_irc_target(join_match.group(2))", source)

    def test_part_takes_any_target(self):
        source = self.source()

        self.assertIn(r'PART (\S+)"', source)
        self.assertIn("is_valid_irc_target(part_match.group(2))", source)

    def test_the_names_reply_takes_any_channel(self):
        """The one that mattered most. 353 populates config.channel_users,
        which dcc.py treats as proof a user is present before it dispatches -
        so a channel whose NAMES never parsed had every send to it refused,
        on top of every command being ignored."""
        source = self.source()

        self.assertIn(r'353\s+\S+\s+(?:[=*@]\s+)?(\S+)\s+:(.+)$', source)
        self.assertIn("is_valid_irc_target(name_match.group(1))", source)

    def test_the_names_reply_is_no_longer_skip_to_the_first_hash(self):
        self.assertNotIn(r'" 353 [^#]+', "\n".join(self.code_lines()))


if __name__ == "__main__":
    unittest.main()
