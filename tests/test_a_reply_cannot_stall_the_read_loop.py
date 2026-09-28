"""A private message cannot stall the IRC read loop (audit of 2026-09-27).

fetch_replies.classify() reads every private NOTICE and message the bot gets,
on the read loop, to see whether another file server is answering a request
of ours. Its rules were regexes of the shape `(?<!\\w)w1.*?w2.*?...wN`, and a
line that repeats the first words without ever completing a rule made them
backtrack polynomially: 450 characters took over 3 seconds, and a few such
lines - from anybody, with no ban or flood check in front - left the server's
PINGs unanswered until it dropped the bot.

Two fixes, each tested here: the rules match in one pass with str.find(),
answering exactly what the regexes did; and a line from somebody we are not
waiting on is not classified at all.
"""

import os
import random
import re
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import fetch_replies  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

# What the old matcher was, kept here as the reference the new one must agree
# with - not used by the daemon.
def _old_regex(pattern):
    parts = [re.escape(word) for word in pattern.split()]
    return re.compile(r"(?<!\w)" + r".*?".join(parts), re.IGNORECASE | re.DOTALL)


# A line built to make the old rules backtrack: the SpR "You are in que now
# with in rank." words over and over, never completing.
def _hostile(length):
    return (" Youareinquenowwithinin" * 100)[:length]


class ItIsLinear(unittest.TestCase):
    def test_the_hostile_line_is_answered_at_once(self):
        """The old matcher took ~3 s on this at 450 characters, ~7 s at 510.
        Half a second is a bound no working machine gets near."""
        for length in (450, 510, 5000):
            started = time.perf_counter()
            fetch_replies.classify(_hostile(length))
            self.assertLess(time.perf_counter() - started, 0.5, length)

    def test_only_a_line_s_length_is_looked_at(self):
        reply = fetch_replies.classify("Request Accepted Position: 3 OmenServE " + "x" * 5000)
        self.assertIsNotNone(reply)
        self.assertLessEqual(len(reply.text), fetch_replies.MAX_REPLY_CHARS)


class ItAnswersWhatTheRegexesDid(unittest.TestCase):
    def test_against_the_old_regexes_on_many_lines(self):
        rng = random.Random(926)
        alphabet = "abcdefghijklmnopqrstuvwxyz ABCDEF_#:.,'0123456789"
        for _outcome, words, _position in fetch_replies._RULES:
            new = fetch_replies._words(words)
            old = _old_regex(words)
            for _ in range(200):
                pieces = []
                for word in words.split():
                    if rng.random() < 0.85:
                        pieces.append(rng.choice([word, word.upper(), word.lower()]))
                    pieces.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4))))
                if rng.random() < 0.2:
                    rng.shuffle(pieces)
                line = "".join(rng.choice(["", " ", "x", "_"]) + piece for piece in pieces)
                self.assertEqual(new.search(line), bool(old.search(line)), (words, line))

    def test_the_first_word_starts_a_word(self):
        rule = fetch_replies._words("Request Accepted Position:")
        self.assertTrue(rule.search("Request Accepted Position: 4"))
        self.assertTrue(rule.search("OmeN - Request Accepted Position: 4"))
        self.assertFalse(rule.search("xRequest Accepted Position: 4"))
        self.assertFalse(rule.search("_Request Accepted Position: 4"))
        self.assertTrue(rule.search("xRequest ... Request Accepted Position: 4"), "a later start still counts")

    def test_a_word_cannot_be_found_inside_the_one_before_it(self):
        """Each word is looked for AFTER the end of the one before, as the
        regex did: "x ab b" needs a "b" after "ab", not the one inside it."""
        rule = fetch_replies._words("x ab b")
        self.assertFalse(rule.search("x ab"))
        self.assertEqual(rule.search("x ab"), bool(_old_regex("x ab b").search("x ab")))
        self.assertTrue(rule.search("x ab b"))

    def test_in_order_and_any_case(self):
        rule = fetch_replies._words("You already have in my queue")
        self.assertTrue(rule.search("YOU ALREADY HAVE song.mp3 IN MY QUEUE"))
        self.assertFalse(rule.search("in my queue you already have"))


class ANonsenseLineFromAStrangerIsNotRead(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.read = []
        real = fetch_replies.classify
        fetch_replies.classify = lambda text: self.read.append(text) or real(text)
        self.addCleanup(setattr, fetch_replies, "classify", real)

    def test_nothing_waiting_on_the_sender_nothing_classified(self):
        self.assertIsNone(dcc_fetch.handle_bot_reply("SomeStranger", _hostile(450)))
        self.assertEqual(self.read, [])

    def test_a_bot_we_are_waiting_on_is_still_read(self):
        queue = dcc_fetch._ensure_fetch_queue()
        queue["r1"] = {"id": "r1", "bot": "PackBot", "state": "offered", "request_type": "file",
                       "requested_filename": "Some Track.mp3", "requested_at": 1.0}
        outcome = dcc_fetch.handle_bot_reply("PackBot", "Request Accepted Position: 5 OmenServE")
        self.assertEqual(outcome, "queued")
        self.assertEqual(len(self.read), 1)
        self.assertEqual(queue["r1"]["queue_position"], 5)


if __name__ == "__main__":
    unittest.main()
