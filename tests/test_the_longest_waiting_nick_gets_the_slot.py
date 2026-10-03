"""A freed slot goes to the nick that has waited longest (#1032).

The nick that had just finished was handed its own next file straight away,
so a nick with a long queue took every slot in turn and the nicks behind it
waited for that queue to run dry. Now the finishing nick goes to the back of
the line, and a new request does not jump ahead of nicks already waiting.
"""

import io
import itertools
import os
import sys
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests import test_a_nick_change_mid_transfer_keeps_the_slot_and_the_queue as renamed  # noqa: E402
from tests import test_complete_means_the_receiver_acked_it as ack  # noqa: E402
from tests.support import queue_row  # noqa: E402

OTHER = feed.OTHER
NAMES = ["A.flac", "B.flac", "C.flac", "D.flac", "E.flac"]


class Case(feed.ServesARealRequest):
    def setUp(self):
        super().setUp()
        for name in NAMES:
            with io.open(os.path.join(self.tree.music, name), "wb") as handle:
                handle.write(b"\x00" * 4096)
        self.set_config(MAX_DCC_SLOTS=1, transfers_paused=False)
        config.channel_users[OTHER] = {"dave", "erin", "frank"}
        # A clock that only moves forward, so two events never share a stamp
        # on a platform with a coarse one.
        ticks = itertools.count(1000)
        patch = mock.patch.object(dcc.time, "time", lambda: float(next(ticks)))
        patch.start()
        self.addCleanup(patch.stop)

    def running(self):
        return [tx["user"] for tx in config.active_transfers]

    def queued(self, user):
        return [row["file"] for row in config.dcc_queue.get(user, [])]

    def finish(self, user):
        config.active_transfers[:] = [tx for tx in config.active_transfers if tx["user"] != user]
        config.user_processing_lock.discard(user)
        dcc.go_to_the_back(user)

    def pick_up(self, user):
        self.quietly(dcc.check_queue_and_send, user)

    def started(self):
        return [(args[1], args[3]) for name, args in feed.InlineThread.dispatched if name == "start_dcc_send"]


class TheFinishedNickGoesToTheBack(Case):
    def test_the_slot_goes_to_the_nick_that_waited_not_the_one_that_finished(self):
        self.request("A.flac", user="dave")
        self.request("B.flac", user="dave")
        self.request("C.flac", user="erin")
        self.request("D.flac", user="frank")
        self.assertEqual(self.running(), ["dave"])
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.running(), ["erin"])

    def test_and_so_on_round_the_line(self):
        self.request("A.flac", user="dave")
        self.request("B.flac", user="dave")
        self.request("C.flac", user="erin")
        self.request("D.flac", user="frank")
        order = []
        for finished in ("dave", "erin", "frank"):
            self.finish(finished)
            self.pick_up(finished)
            order.extend(self.running())
        self.assertEqual(order, ["erin", "frank", "dave"])

    def test_with_nobody_else_waiting_the_same_nick_carries_on(self):
        self.request("A.flac", user="dave")
        self.request("B.flac", user="dave")
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.running(), ["dave"])
        self.assertEqual(self.started()[-1], ("dave", "B.flac"))

    def test_a_trigger_for_a_nick_that_waited_less_still_gives_the_slot_to_the_longest(self):
        self.request("A.flac", user="dave")
        self.request("C.flac", user="erin")
        self.request("D.flac", user="frank")
        self.finish("dave")
        self.pick_up("frank")
        self.assertEqual(self.running(), ["erin"])


class ANewRequestDoesNotJumpTheLine(Case):
    def test_it_queues_behind_a_nick_that_is_waiting(self):
        self.request("A.flac", user="dave")
        self.request("C.flac", user="erin")
        self.finish("dave")
        self.request("D.flac", user="frank")
        self.assertEqual(self.running(), [])
        self.assertEqual(self.queued("frank"), ["D.flac"])
        self.pick_up("dave")
        self.assertEqual(self.running(), ["erin"])

    def test_it_sends_at_once_when_nobody_is_waiting(self):
        self.request("A.flac", user="frank")
        self.assertEqual(self.running(), ["frank"])

    def test_a_nick_that_is_absent_is_not_waited_for(self):
        config.channel_users[OTHER] = {"dave", "frank"}
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        self.request("A.flac", user="frank")
        self.assertEqual(self.running(), ["frank"])

    def test_a_frozen_nick_is_not_waited_for(self):
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        config.frozen_queues["erin"] = 1
        self.request("A.flac", user="frank")
        self.assertEqual(self.running(), ["frank"])


class TheSweepKeepsTheSameOrder(Case):
    def test_it_takes_the_longest_waiting_whatever_the_insertion_order(self):
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="A.flac")]
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        config.dcc_queue["frank"] = [queue_row(user="frank", filename="D.flac")]
        runtime.queue_waiting_since.update({"dave": 30.0, "erin": 10.0, "frank": 20.0})
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.running(), ["erin"])

    def test_a_folder_pack_being_made_elsewhere_does_not_hold_back_a_plain_file(self):
        """While another pack is being made erin's could not start; the packer's
        release wakes it, so dave is not held back for it. (A pack that CAN start
        takes its turn: test_a_blocked_pack_is_woken.)"""
        self.set_config(rar_inprogress=True)
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="Album.rar", is_unpacked_rar_folder=True,
                                              is_temporary_zip=True)]
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="A.flac")]
        runtime.queue_waiting_since.update({"erin": 1.0, "dave": 2.0})
        self.pick_up("dave")
        self.assertEqual(self.running(), ["dave"])


class TheGateOnlyWaitsForNicksTheSweepCanStart(Case):
    def test_a_nick_holding_a_folder_pack_does_not_hold_a_newcomer_back(self):
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="Album.rar", is_unpacked_rar_folder=True, is_temporary_zip=True)]
        self.request("A.flac", user="frank")
        self.assertEqual(self.running(), ["frank"])

    def test_a_folder_pack_is_not_counted_as_waiting(self):
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="Album.rar", is_unpacked_rar_folder=True, is_temporary_zip=True)]
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="A.flac")]
        self.assertEqual(dcc.nicks_waiting_for_a_slot(plain_files_only=True), ["dave"])
        self.assertEqual(dcc.nicks_waiting_for_a_slot(), ["erin", "dave"])

    def test_a_free_slot_beyond_the_waiting_nicks_is_used_at_once(self):
        self.set_config(MAX_DCC_SLOTS=3)
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        self.request("A.flac", user="frank")
        self.assertEqual(self.running(), ["frank"])

    def test_as_many_waiting_nicks_as_free_slots_leave_none_for_a_newcomer(self):
        self.set_config(MAX_DCC_SLOTS=3)
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="A.flac")]
        config.active_transfers.append({"user": "gina", "file": "X.flac", "bytes_sent": 0})
        self.request("D.flac", user="frank")
        self.assertEqual(self.running(), ["gina"])
        self.assertEqual(self.queued("frank"), ["D.flac"])

    def test_one_slot_and_one_waiting_nick_queues_the_newcomer(self):
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        self.request("A.flac", user="frank")
        self.assertEqual(self.running(), [])
        self.assertEqual(self.queued("frank"), ["A.flac"])


class TheWaitIsStamped(Case):
    def test_the_first_queued_file_starts_the_wait_and_the_next_does_not_restart_it(self):
        self.request("A.flac", user="dave")
        self.request("B.flac", user="erin")
        first = runtime.queue_waiting_since["erin"]
        self.request("C.flac", user="erin")
        self.assertEqual(runtime.queue_waiting_since["erin"], first)

    def test_a_nick_that_empties_its_queue_starts_a_new_wait_next_time(self):
        self.request("A.flac", user="dave")
        self.request("B.flac", user="erin")
        first = runtime.queue_waiting_since["erin"]
        config.dcc_queue.pop("erin")
        self.request("C.flac", user="erin")
        self.assertGreater(runtime.queue_waiting_since["erin"], first)

    def test_a_finished_send_puts_the_nick_at_the_back(self):
        self.request("A.flac", user="dave")
        self.request("B.flac", user="dave")
        self.request("B.flac", user="erin")
        before = runtime.queue_waiting_since["erin"]
        self.finish("dave")
        self.assertGreater(runtime.queue_waiting_since["dave"], before)

    def test_a_nick_nothing_has_stamped_counts_as_waiting_longest(self):
        self.assertEqual(dcc.queue_waiting_since("unknown"), 0.0)


class TheStampsAreTidy(Case):
    def test_a_finished_nick_with_nothing_left_keeps_no_stamp(self):
        self.request("A.flac", user="dave")
        self.assertNotIn("dave", runtime.queue_waiting_since)
        self.finish("dave")
        self.assertNotIn("dave", runtime.queue_waiting_since)

    def test_the_sweep_forgets_the_stamp_of_a_queue_that_is_gone(self):
        runtime.queue_waiting_since.update({"ghost": 1.0, "erin": 2.0})
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        self.pick_up("system_next_trigger_fallback")
        self.assertNotIn("ghost", runtime.queue_waiting_since)

    def test_a_folder_request_starts_the_wait_like_a_file_does(self):
        self.fill_the_slots()
        self.request("!rar Metallica/Black Album (1991)", user="dave")
        self.assertIn("dave", runtime.queue_waiting_since)
        first = runtime.queue_waiting_since["dave"]
        self.request("A.flac", user="dave")
        self.assertEqual(runtime.queue_waiting_since["dave"], first)

    def test_presence_is_read_once_however_many_nicks_wait(self):
        for nick in ("dave", "erin", "frank"):
            config.dcc_queue[nick] = [queue_row(user=nick, filename="A.flac")]
        with mock.patch.object(dcc, "nicks_in_our_channels", wraps=dcc.nicks_in_our_channels) as read:
            dcc.nicks_waiting_for_a_slot()
        self.assertEqual(read.call_count, 1)


@unittest.skipUnless(renamed.loopback_is_usable(), "needs a loopback socket")
class ARealSendPutsTheNickAtTheBack(ack.ARealReceiver):
    # ARealReceiver carries its own tests; only its receiver plumbing is wanted.
    def test_the_finally_of_a_real_send_moves_the_stamp(self):
        nick = ack.USER.lower()
        config.channel_users = {"#somechannel": {ack.USER}}
        config.user_processing_lock = {nick}
        config.dcc_queue[nick] = [queue_row(user=ack.USER, filename="Next.flac")]
        config.frozen_queues[nick] = 0          # the wake must not start the next row
        runtime.queue_waiting_since[nick] = 5.0
        irc_sock, sender = self.start_send()
        self.receive(self.connect(irc_sock.port()))
        sender.join(30)
        self.assertGreater(runtime.queue_waiting_since[nick], 5.0)
        self.assertNotIn(nick, config.user_processing_lock)


for _name in [n for n in dir(ack.ARealReceiver) if n.startswith("test")]:
    setattr(ARealSendPutsTheNickAtTheBack, _name, None)


if __name__ == "__main__":
    unittest.main()
