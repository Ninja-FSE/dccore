"""A grab the queue refused is not a try, and a list that arrives starts over (#967).

Automatic list grabbing gives a bot three tries, then "gave up" - on disk, so a
restart keeps it. The try was counted BEFORE the enqueue: with FETCHED_FILES_DIR
missing at boot (503) or an operator's own fetch from that bot outstanding
(409), three refusals marked every candidate "gave up" though nothing had been
asked, and it stayed so once the cause was gone. And the count was for life: a
bot whose list arrived, was later cleared by the purge of offline bots, and was
grabbed again, gave up after its third answered grab.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import list_fetch  # noqa: E402
import list_grab  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_lists_are_grabbed_automatically_on_autogets_rules as rules  # noqa: E402

NOW = rules.NOW
APART = 30 + list_grab.GRAB_COOLDOWN_SECONDS


class ARefusalIsNotATry(rules.GrabCase):
    def setUp(self):
        super().setUp()
        self.set_config(AUTO_GRAB_EVERY_MINUTES=0)
        self.advertise("PackBot")

    def test_three_refusals_do_not_give_up(self):
        self.answer = (503, {"error": "Fetching is unavailable"})
        at = NOW
        for _ in range(3):
            self.assertEqual(self.grab(at), "refused")
            at += APART
        self.assertNotEqual(list_grab._why_not("packbot", {"files": 5000}, {"packbot"}, at), "gave up")
        self.assertEqual(list_grab._state()["tries"]["packbot"].get("tries"), 0)

        self.answer = (200, {})
        self.assertEqual(self.grab(at), "asked")
        self.assertTrue(any("try 1 of 3" in line for line in self.lines))

    def test_a_refusal_still_waits_its_turn(self):
        """Not asked again on the next tick: spaced as an ask would be."""
        self.answer = (409, {"error": "busy"})
        self.assertEqual(self.grab(NOW), "refused")
        self.assertEqual(self.tick(NOW + 60), "nothing", "cooling down")


class AnArrivedListStartsOver(rules.GrabCase):
    def setUp(self):
        super().setUp()
        self.set_config(AUTO_GRAB_EVERY_MINUTES=0)
        self.advertise("PackBot")

    def test_the_tries_start_over(self):
        at = NOW
        for _ in range(3):
            self.assertEqual(self.grab(at), "asked")
            at += APART
        self.assertEqual(self.tick(at), "nothing", "gave up - the premise")

        list_grab.note_list_arrived("PackBot")
        self.assertNotIn("packbot", list_grab._state()["tries"])
        self.assertEqual(self.grab(at), "asked")

    def test_a_list_fetched_by_hand_is_no_longer_removed(self):
        list_grab.note_removed_by_hand("PackBot")
        self.assertEqual(self.tick(NOW), "nothing")
        list_grab.note_list_arrived("PackBot")
        self.assertNotIn("packbot", list_grab._state()["removed"])
        self.assertEqual(self.grab(NOW), "asked")

    def test_the_arrival_is_what_tells_it(self):
        """process_fetched_list_zip() is where a list arrives, whoever asked."""
        seen = []
        real_unlocked = list_fetch._process_fetched_list_zip_unlocked
        real_note = list_grab.note_list_arrived
        list_fetch._process_fetched_list_zip_unlocked = lambda bot, path, channel=None, secondary=False: (True, None)
        list_grab.note_list_arrived = seen.append
        self.addCleanup(setattr, list_fetch, "_process_fetched_list_zip_unlocked", real_unlocked)
        self.addCleanup(setattr, list_grab, "note_list_arrived", real_note)

        list_fetch.process_fetched_list_zip("PackBot", "unused.zip")
        self.assertEqual(seen, ["PackBot"])

        list_fetch._process_fetched_list_zip_unlocked = lambda bot, path, channel=None, secondary=False: (False, "not a list")
        list_fetch.process_fetched_list_zip("PackBot", "unused.zip")
        self.assertEqual(seen, ["PackBot"], "an unusable list is not an arrival")


if __name__ == "__main__":
    unittest.main()
