"""A passive offer for a queued request waits for a free fetch slot.

A request queued at another bot holds no slot of ours, so meanwhile the
dispatcher asks other bots; its offer is still admitted when its turn comes,
since refusing it would throw its place in that queue away. But a PASSIVE offer
opens a listener in the DCC port range the bot's own sends to its users share,
and queues at several bots coming due together could take every port in it.
Past MAX_FETCH_SLOTS, a passive offer for a queued row is now not taken: the
row goes back to pending and is asked for again once a slot is free. An active
offer, which costs no port, is admitted as before.
"""

import time
import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_a_file_too_big_for_the_disk_waits_for_room as room  # noqa: E402

SIZE = 1024 * 1024


class SlotsCase(room.RoomCase):
    def setUp(self):
        super().setUp()
        self.set_config(MAX_FETCH_SLOTS=1)

    def queued(self, name):
        rid = self.ask("file", name)
        config.fetch_queue[rid].update(state="queued", queue_position=2, reply="Added ... position #2")
        return rid

    def passive(self, name):
        dcc_fetch.handle_incoming_offer(None, "ServerOne", f"DCC SEND {name} 2130706433 0 {SIZE} 77")

    def active(self, name):
        dcc_fetch.handle_incoming_offer(None, "ServerOne", f"DCC SEND {name} 2130706433 55000 {SIZE}")


class WithEverySlotInUse(SlotsCase):
    def setUp(self):
        super().setUp()
        self.waiting = self.queued("Waiting.flac")
        self.busy = self.ask("file", "Busy.flac")      # the one slot, taken

    def test_a_passive_offer_for_the_queued_row_is_not_listened_for(self):
        self.passive("Waiting.flac")
        row = config.fetch_queue[self.waiting]
        self.assertEqual(self.taken, [], "no listener opened")
        self.assertEqual(row["state"], "pending", "asked for again once a slot is free")
        self.assertNotIn("queue_position", row)

    def test_a_late_passive_offer_for_a_row_given_up_on_is_not_listened_for_either(self):
        """A late offer takes a failed row, which holds no slot of ours."""
        row = config.fetch_queue[self.waiting]
        row.update(state="failed", reason="no response", offered_at=time.time())
        self.passive("Waiting.flac")
        self.assertEqual(self.taken, [], "no listener opened")
        self.assertEqual(config.fetch_queue[self.waiting]["state"], "pending")

    def test_it_is_asked_again_when_the_slot_frees(self):
        self.passive("Waiting.flac")
        self.assertEqual(self.asked_again(), [], "not while the slot is in use")
        config.fetch_queue[self.busy]["state"] = "complete"
        said = self.asked_again()
        self.assertEqual(len(said), 1)
        self.assertIn("Waiting.flac", said[0])

    def test_an_active_offer_is_still_admitted(self):
        """It costs no port, and refusing it would lose the place in their queue."""
        self.active("Waiting.flac")
        self.assertEqual(self.taken, ["_run_transfer"])


class WithASlotFree(SlotsCase):
    def test_a_passive_offer_for_a_queued_row_is_listened_for(self):
        rid = self.queued("Waiting.flac")
        self.passive("Waiting.flac")
        self.assertEqual(self.taken, ["_serve_passive_offer"])
        self.assertEqual(config.fetch_queue[rid]["state"], "listening")

    def test_an_offered_row_brings_its_own_slot(self):
        """Not queued: it has held its slot since it was asked for - even when
        the limit was lowered after it was, and the slots now count over."""
        self.set_config(MAX_FETCH_SLOTS=2)
        self.ask("file", "Asked.flac")
        self.ask("file", "Other.flac")
        self.set_config(MAX_FETCH_SLOTS=1)
        self.passive("Asked.flac")
        self.assertEqual(self.taken, ["_serve_passive_offer"])


if __name__ == "__main__":
    unittest.main()
