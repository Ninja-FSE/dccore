"""#1304: dcc.claim_next_in_line(), step 3 of splitting dcc.py.

Section B of check_queue_and_send() - the sweep under queue_lock that gives
a free slot to the next in line - reachable until now only by driving a
whole dispatch. Called directly here. It claims and returns; it sends
nothing, so every test reads what it claimed and what it left alone.

Every nick, channel and file here is invented.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def row(nick, name, list_row=False, pack=False):
    entry = {"file": name, "path": os.path.join("library", name), "channel": "#somechannel",
             "user_raw": nick, "is_unpacked_rar_folder": pack}
    if list_row:
        entry[dcc.LIST_ROW_KEY] = True
    return entry


class TheSweep(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(MAX_DCC_SLOTS=2, rar_inprogress=False)
        self.addCleanup(runtime.queue_waiting_since.clear)
        self.reset()

    def reset(self):
        config.dcc_queue.clear()
        config.active_transfers.clear()
        config.frozen_queues.clear()
        config.user_processing_lock = set()
        config.channel_users.clear()
        config.channel_users["#somechannel"] = {"alfa", "bravo", "charlie"}
        runtime.queue_waiting_since.clear()

    def waiting(self, nick, since, *rows):
        config.dcc_queue[nick] = list(rows) or [row(nick, nick + ".flac")]
        runtime.queue_waiting_since[nick] = since

    def claim(self):
        return dcc.claim_next_in_line()

    def test_the_longest_wait_wins_and_is_claimed(self):
        self.waiting("alfa", 200.0)
        self.waiting("bravo", 100.0)

        promoted, key, pack, absent = self.claim()

        self.assertEqual((promoted[0], promoted[2], key, pack, absent), ("bravo", "bravo.flac", "bravo", None, []))
        self.assertIn("bravo", config.user_processing_lock)
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["bravo"])
        self.assertIs(config.active_transfers[0]["queue_row"], config.dcc_queue["bravo"][0])

    def test_a_list_at_a_queues_head_goes_first(self):
        self.waiting("alfa", 100.0)
        self.waiting("bravo", 200.0, row("bravo", "alfa-list.zip", list_row=True))

        promoted, key, _pack, _absent = self.claim()

        self.assertEqual(key, "bravo")
        self.assertTrue(config.active_transfers[0].get("list_went_first"), "the cap must count it")

    def test_not_while_another_list_that_went_first_is_sending(self):
        config.active_transfers.append({"user": "charlie", "list_went_first": True})
        self.waiting("alfa", 100.0)
        self.waiting("bravo", 200.0, row("bravo", "alfa-list.zip", list_row=True))

        self.assertEqual(self.claim()[1], "alfa")

    def test_held_frozen_and_already_sending_users_are_passed_over(self):
        for how in ("held", "frozen", "sending"):
            with self.subTest(how=how):
                self.reset()
                self.waiting("alfa", 100.0)
                self.waiting("bravo", 200.0)
                if how == "held":
                    config.user_processing_lock.add("alfa")
                elif how == "frozen":
                    config.frozen_queues["alfa"] = 1.0
                else:
                    config.active_transfers.append({"user": "Alfa"})
                self.assertEqual(self.claim()[1], "bravo")

    def test_an_absent_user_is_returned_for_a_freeze_not_claimed(self):
        config.channel_users["#somechannel"] = {"bravo"}
        self.waiting("alfa", 100.0)
        self.waiting("bravo", 200.0)

        promoted, key, _pack, absent = self.claim()

        self.assertEqual(key, "bravo")
        self.assertEqual(absent, [("alfa", "#somechannel")])
        self.assertNotIn("alfa", config.user_processing_lock)

    def test_a_pack_whose_turn_has_come_is_woken_with_a_slot_kept(self):
        self.waiting("alfa", 100.0, row("alfa", "Some Album", pack=True))
        self.waiting("bravo", 200.0)

        promoted, key, pack, _absent = self.claim()

        self.assertEqual(pack, "alfa")
        self.assertEqual(key, "bravo", "two slots: one kept for the pack, one for bravo")

    def test_with_one_slot_the_pack_keeps_it(self):
        self.set_config(MAX_DCC_SLOTS=1)
        self.waiting("alfa", 100.0, row("alfa", "Some Album", pack=True))
        self.waiting("bravo", 200.0)

        promoted, key, pack, _absent = self.claim()

        self.assertEqual((promoted, key, pack), (None, None, "alfa"))
        self.assertEqual(config.active_transfers, [])

    def test_while_another_pack_is_made_the_sweep_looks_further(self):
        config.rar_inprogress = True
        self.waiting("alfa", 100.0, row("alfa", "Some Album", pack=True))
        self.waiting("bravo", 200.0)

        promoted, key, pack, _absent = self.claim()

        self.assertEqual((key, pack), ("bravo", None))

    def test_full_slots_claim_nothing(self):
        config.active_transfers.extend([{"user": "x"}, {"user": "y"}])
        self.waiting("alfa", 100.0)

        self.assertEqual(self.claim(), (None, None, None, []))
        self.assertNotIn("alfa", config.user_processing_lock)


if __name__ == "__main__":
    unittest.main()
