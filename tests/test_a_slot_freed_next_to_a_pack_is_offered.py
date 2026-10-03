"""A slot freed next to a folder pack is offered to the nick that waited longest (#1038).

check_queue_and_send() returned from the pack branch of the nick that had just
finished without looking further: [RAR-HOLD] (another pack is being made),
[RAR-BLOCK] (the nick is already locked), the packer being started, and an
absent nick being frozen. A pack holds no slot while it packs, so the slot
stood idle - and with #1032 a newcomer was kept out of it for a waiter nothing
would dispatch. Now those exits go on to the global sweep.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
from tests.support import queue_row  # noqa: E402

import tests.test_the_longest_waiting_nick_gets_the_slot as fair  # noqa: E402
from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests import test_path_security as path_security  # noqa: E402


def a_pack(user):
    return queue_row(user=user, filename="Album.rar", is_unpacked_rar_folder=True, is_temporary_zip=True)


class Case(fair.Case):
    def setUp(self):
        super().setUp()
        # The packer is recorded, not run: only who gets the slot is read.
        real = path_security.InlineThread.RUN_INLINE
        path_security.InlineThread.RUN_INLINE = ()
        self.addCleanup(setattr, path_security.InlineThread, "RUN_INLINE", real)
        config.rar_inprogress = False
        self.addCleanup(setattr, config, "rar_inprogress", False)
        config.dcc_queue["dave"] = [a_pack("dave")]
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        runtime.queue_waiting_since["erin"] = 500.0
        runtime.queue_waiting_since["dave"] = 600.0

    def dispatched(self, name):
        return [args[-1] for target, args in feed.InlineThread.dispatched if target == name]


class TheFreedSlotIsNotLeftIdle(Case):
    def test_another_pack_being_made(self):
        config.rar_inprogress = True
        self.pick_up("dave")
        self.assertEqual(self.started(), [("erin", "C.flac")])

    def test_the_nick_already_locked(self):
        config.user_processing_lock.add("dave")
        self.pick_up("dave")
        self.assertEqual(self.started(), [("erin", "C.flac")])

    def test_the_packer_starting(self):
        self.pick_up("dave")
        self.assertEqual(len(self.dispatched("inline_rar_packer")), 1, "the packer was started")
        self.assertEqual(self.started(), [("erin", "C.flac")])

    def test_an_absent_nick_being_frozen(self):
        config.channel_users[feed.OTHER].discard("dave")
        self.pick_up("dave")
        self.assertIn("dave", config.frozen_queues)
        self.assertEqual(self.started(), [("erin", "C.flac")])

    def test_a_newcomer_queues_behind_the_nick_that_is_now_sending(self):
        config.rar_inprogress = True
        self.pick_up("dave")
        self.request("D.flac", user="frank")
        self.assertEqual(self.running(), ["erin"])
        self.assertEqual(self.queued("frank"), ["D.flac"])


class NothingIsOverbooked(Case):
    def test_with_every_slot_busy_nobody_starts(self):
        config.rar_inprogress = True
        config.active_transfers.append({"user": "someoneelse", "file": "X.flac", "bytes_sent": 0})
        self.pick_up("dave")
        self.assertEqual(self.started(), [])
        self.assertEqual(len(config.active_transfers), 1)

    def test_a_second_slot_is_not_spent_on_the_pack_itself(self):
        self.pick_up("dave")
        self.assertNotIn("dave", self.running())
        self.assertEqual(len(config.active_transfers), 1)

    def test_a_pack_that_waited_longer_is_still_not_started_by_the_sweep(self):
        runtime.queue_waiting_since["dave"] = 400.0
        config.rar_inprogress = True
        self.pick_up("dave")
        self.assertEqual(self.dispatched("check_queue_and_send"), [])
        self.assertEqual(self.started(), [("erin", "C.flac")])


if __name__ == "__main__":
    unittest.main()
