"""A reply is about the longest name it carries (#974).

Two requests to one bot, "Intro.mp3" and "Band - Intro.mp3". The reply "Sorry,
but Band - Intro.mp3 is not found" contains both names, and the older request -
"Intro.mp3" - was taken as the one it named: failed as refused, while the one
it was really about waited out its timeout. A queued reply moved the wrong row
the same way.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_fetch_queue_waits_and_paces_itself as paces  # noqa: E402


class TwoNamesOneInsideTheOther(paces.QueueCase):
    def ask(self, *names):
        rids = [dcc_fetch.enqueue_fetch("ServerOne", name) for name in names]
        for n, rid in enumerate(rids):
            config.fetch_queue[rid]["requested_at"] = 1000.0 + n
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"] * len(rids))
        return rids

    def test_a_refusal_fails_the_request_it_names(self):
        short, long_ = self.ask("Intro.mp3", "Band - Intro.mp3")
        dcc_fetch.handle_bot_reply("ServerOne", "Sorry, but Band - Intro.mp3 is not found")
        self.assertEqual(self.states([short, long_]), ["offered", "failed"])

    def test_whichever_was_asked_first(self):
        long_, short = self.ask("Band - Intro.mp3", "Intro.mp3")
        dcc_fetch.handle_bot_reply("ServerOne", "Sorry, but Band - Intro.mp3 is not found")
        self.assertEqual(self.states([long_, short]), ["failed", "offered"])

    def test_the_short_name_alone_is_still_the_short_one(self):
        short, long_ = self.ask("Intro.mp3", "Band - Intro.mp3")
        dcc_fetch.handle_bot_reply("ServerOne", "Sorry, but Intro.mp3 is not found")
        self.assertEqual(self.states([short, long_]), ["failed", "offered"])

    def test_a_queued_reply_moves_the_request_it_names(self):
        short, long_ = self.ask("Intro.mp3", "Band - Intro.mp3")
        dcc_fetch.handle_bot_reply("ServerOne", "Added Band - Intro.mp3 to your personal queue at position #2 of 9.")
        self.assertEqual(self.states([short, long_]), ["offered", "queued"])

    def test_two_different_names_in_one_refusal_touch_neither(self):
        first, second = self.ask("One.mp3", "Two.mp3")
        dcc_fetch.handle_bot_reply("ServerOne", "Sorry, but One.mp3 or Two.mp3 is not found")
        self.assertEqual(self.states([first, second]), ["offered", "offered"])


if __name__ == "__main__":
    unittest.main()
