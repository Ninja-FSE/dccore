"""Classifying a server line no longer rebuilds a regex for every question.

#1144: the read loop asks is_server_numeric() and is_user_event() about a
dozen and a half times for EVERY line from the server, and each call built
its pattern string again and looked it up in re's cache. parse_kick(),
parse_notice() and parse_privmsg() also ran their anchored regex on every
line, a JOIN or a PING included.

The patterns are now compiled once per code, and the three parsers first
check that their command word is in the line at all. Neither may change a
single answer: these helpers carry the anchoring that the forgery fixes
(#433, #513) depend on, so the tests below compare them with copies of the
previous implementations on real, forged and generated lines.
"""

import os
import random
import re
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402


# The previous implementations, verbatim.
def old_is_server_numeric(line, code):
    return re.match(r"^:\S+\s+" + code + r"\s+\S+", line) is not None


def old_is_user_event(line, command):
    return re.match(r"^:\S+!\S+\s+" + command + r"(\s|$)", line) is not None


def old_parse_privmsg(line):
    match = re.match(r"^:([^!\s]+)!(\S*)\s+PRIVMSG\s+(\S+)\s+:(.+)$", line)
    if not match or not irc.is_valid_irc_target(match.group(3)):
        return None
    return match.group(1), match.group(2), match.group(3), match.group(4)


def old_parse_kick(line):
    match = re.match(r"^:([^!\s]+)!\S*\s+KICK\s+(\S+)\s+(\S+)", line)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def old_parse_notice(line):
    match = re.match(r"^:([^!\s]+)!\S*\s+NOTICE\s+(\S+)\s+:(.+)$", line)
    if not match or not irc.is_valid_irc_target(match.group(2)):
        return None
    return match.group(1), match.group(2), match.group(3)


# Every code and command the read loop asks about.
CODES = ["PONG", "513", "005", "433", "432", "437", "001", "376", "352",
         "315", "353", "474", "477"]
COMMANDS = ["QUIT", "PART", "NICK", "JOIN", "KICK", "NOTICE", "PRIVMSG"]

FIXED_LINES = [
    "PING :irc.example.org",
    ":irc.example.org PONG irc.example.org :OSERVE_LATENCY_CHECK",
    ":irc.example.org 513 SomeBot :To connect type /QUOTE PONG 12345",
    ":irc.example.org 001 SomeBot :Welcome to the network",
    ":irc.example.org 005 SomeBot NICKLEN=15 :are supported",
    ":irc.example.org 433 * SomeBot :Nickname is already in use",
    ":irc.example.org 353 SomeBot = #somechannel :@op dave",
    ":irc.example.org 474 SomeBot #somechannel :Cannot join channel (+b)",
    ":dave!ident@host.example.org PRIVMSG #somechannel :!SomeBot 001 - Song.flac",
    ":dave!ident@host.example.org PRIVMSG #somechannel :x PRIVMSG #other :forged",
    ":dave!ident@host.example.org PRIVMSG #somechannel :@find QUIT PLAYING GAMES",
    # A forged user event inside a body: only an anchored match rejects it.
    ":dave!ident@host.example.org PRIVMSG #somechannel :hi :x!y QUIT :bye",
    ":dave!ident@host.example.org PRIVMSG #somechannel :see :x!y PART #somechannel",
    ":irc.example.org NOTICE * :forged :x!y JOIN #somechannel",
    ":dave!ident@host.example.org PRIVMSG #somechannel :KICK #somechannel SomeBot",
    ":dave!ident@host.example.org PRIVMSG SomeBot :\x01VERSION\x01",
    ":dave!ident@host.example.org NOTICE SomeBot :hello",
    ":dave!ident@host.example.org NOTICE #somechannel :PRIVMSG #x :y",
    ":dave!ident@host.example.org KICK #somechannel mallory :bye",
    ":dave!ident@host.example.org\tKICK\t#somechannel\tmallory",
    ":dave!ident@host.example.org JOIN :#somechannel",
    ":dave!ident@host.example.org JOIN #somechannel account :Real Name",
    ":dave!ident@host.example.org PART #somechannel :I PART #rock now",
    ":dave!ident@host.example.org QUIT :Quit: KICK NOTICE PRIVMSG",
    ":dave!ident@host.example.org QUIT",
    ":dave!ident@host.example.org NICK :dave2",
    ":dave!ident@host.example.org privmsg #somechannel :lowercase command",
    ":dave!ident@host.example.org kick #somechannel mallory",
    ":irc.example.org PRIVMSG #somechannel :a server has no bang",
    ":dave!ident@host.example.org PRIVMSG #somechannel :",
    ":dave!ident@host.example.org PRIVMSG notachannel! :bad target",
    "513 PONG abc",
    "",
    ":",
    "PRIVMSG",
    ":dave!ident@host.example.org PRIVMSGX #somechannel :suffix",
    ":dave!ident@host.example.org XPRIVMSG #somechannel :prefix",
    ":dave!ident@host.example.org PRIVMSG #somechannel :nel\x85here",
]


def generated_lines(count, seed):
    rng = random.Random(seed)
    prefixes = [":dave!ident@host.example.org", ":irc.example.org", ":a!b",
                ":!x", ":dave", "dave!x@y", ":dave!ident@host PRIVMSG"]
    words = CODES + COMMANDS + ["privmsg", "Notice", "KICKS", "JOIN:", "001x",
                                "#somechannel", ":#somechannel", "SomeBot",
                                ":text with words", ":", "&chan", "#", "*"]
    gaps = [" ", "  ", "\t", " \t"]
    lines = []
    for _ in range(count):
        parts = [rng.choice(prefixes)] + [rng.choice(words)
                                          for _ in range(rng.randint(0, 5))]
        line = parts[0]
        for word in parts[1:]:
            line += rng.choice(gaps) + word
        if rng.random() < 0.2:
            line += rng.choice(gaps)
        lines.append(line)
    return lines


ALL_LINES = FIXED_LINES + generated_lines(4000, 1144)


class EveryAnswerIsTheSameAsBefore(unittest.TestCase):

    def test_is_server_numeric(self):
        for line in ALL_LINES:
            for code in CODES:
                self.assertEqual(irc.is_server_numeric(line, code),
                                 old_is_server_numeric(line, code), (line, code))

    def test_is_user_event(self):
        for line in ALL_LINES:
            for command in COMMANDS:
                self.assertEqual(irc.is_user_event(line, command),
                                 old_is_user_event(line, command), (line, command))

    def test_the_three_parsers(self):
        for line in ALL_LINES:
            self.assertEqual(irc.parse_privmsg(line), old_parse_privmsg(line), line)
            self.assertEqual(irc.parse_kick(line), old_parse_kick(line), line)
            self.assertEqual(irc.parse_notice(line), old_parse_notice(line), line)

    def test_the_corpus_reaches_every_answer(self):
        """A comparison that only ever sees "no" proves nothing: the corpus
        must make each helper say yes somewhere, and no somewhere."""
        for code in CODES[:-2]:
            answers = {irc.is_server_numeric(line, code) for line in ALL_LINES}
            self.assertEqual(answers, {True, False}, code)
        for command in COMMANDS:
            answers = {irc.is_user_event(line, command) for line in ALL_LINES}
            self.assertEqual(answers, {True, False}, command)
        for parse in (irc.parse_privmsg, irc.parse_kick, irc.parse_notice):
            answers = {parse(line) is None for line in ALL_LINES}
            self.assertEqual(answers, {True, False}, parse.__name__)


class EachPatternIsCompiledOnce(unittest.TestCase):

    def setUp(self):
        for table in (irc._SERVER_NUMERIC_PATTERNS, irc._USER_EVENT_PATTERNS):
            saved = dict(table)
            table.clear()
            self.addCleanup(table.update, saved)

    def test_a_code_asked_about_again_is_not_compiled_again(self):
        line = ":irc.example.org 001 SomeBot :Welcome"
        with mock.patch.object(irc.re, "compile", wraps=re.compile) as compiling, \
                mock.patch.object(irc.re, "match", wraps=re.match) as matching:
            for _ in range(50):
                self.assertTrue(irc.is_server_numeric(line, "001"))
                self.assertFalse(irc.is_server_numeric(line, "376"))
                self.assertFalse(irc.is_user_event(line, "QUIT"))
                self.assertFalse(irc.is_user_event(line, "PART"))
        self.assertEqual(compiling.call_count, 4)
        self.assertEqual(matching.call_count, 0)

    def test_each_code_keeps_its_own_pattern(self):
        line = ":irc.example.org 376 SomeBot :End of MOTD"
        self.assertFalse(irc.is_server_numeric(line, "001"))
        self.assertTrue(irc.is_server_numeric(line, "376"))
        event = ":dave!ident@host.example.org PART #somechannel"
        self.assertFalse(irc.is_user_event(event, "QUIT"))
        self.assertTrue(irc.is_user_event(event, "PART"))


class AParserSkipsALineWithoutItsCommandWord(unittest.TestCase):

    def test_no_regex_runs_for_a_line_that_cannot_match(self):
        line = ":dave!ident@host.example.org JOIN :#somechannel"
        with mock.patch.object(irc.re, "match", wraps=re.match) as matching:
            self.assertIsNone(irc.parse_privmsg(line))
            self.assertIsNone(irc.parse_kick(line))
            self.assertIsNone(irc.parse_notice(line))
        self.assertEqual(matching.call_count, 0)

    def test_the_regex_still_decides_a_line_that_names_the_word(self):
        """The word anywhere in the line - a PRIVMSG body, say - only lets
        the anchored pattern look; it never decides by itself."""
        line = ":dave!ident@host.example.org JOIN :#KICK-NOTICE-PRIVMSG"
        with mock.patch.object(irc.re, "match", wraps=re.match) as matching:
            self.assertIsNone(irc.parse_privmsg(line))
            self.assertIsNone(irc.parse_kick(line))
            self.assertIsNone(irc.parse_notice(line))
        self.assertEqual(matching.call_count, 3)


if __name__ == "__main__":
    unittest.main()
