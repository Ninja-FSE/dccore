"""A list request takes the next free slot ahead of queued files (#1205).

"@nick" went through handle_download_request() like any file, so with every
slot busy the list was queued and waited behind everyone's albums - while the
list is small, quick to send, and what leads anybody to ask for anything.

Now a list goes to the front of its nick's queue and is offered the next free
slot before the nicks waiting for files and folders. It never interrupts a
send, a nick still has one send at a time, and the cap keeps it honest: only
one list at a time may take a slot that would otherwise have gone to a nick
that waited longer. A list that would have had the slot anyway does not count.
A list is not its nick's turn, so files that nick was waiting for keep their
place. Driven through the real request and dispatch functions.
"""

import io
import os
import threading
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import commands  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
from tests.support import queue_row  # noqa: E402

import tests.test_the_longest_waiting_nick_gets_the_slot as fair  # noqa: E402
from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests import test_path_security as path_security  # noqa: E402
from tests import test_a_nick_change_mid_transfer_keeps_the_slot_and_the_queue as renamed  # noqa: E402
from tests import test_complete_means_the_receiver_acked_it as ack  # noqa: E402

NICKS = {"dave", "erin", "frank", "gina", "hank", "ivan", "judy", "kate", "liam", "mona"}


class Case(fair.Case):
    def setUp(self):
        super().setUp()
        self.list_name = f"{config.LIST_BASE_NAME}-2026-09-01.zip"
        with io.open(os.path.join(self.tree.lists, self.list_name), "wb") as handle:
            handle.write(b"\x00" * 2048)
        config.channel_users[feed.OTHER] = set(NICKS)
        # Packs are recorded, not run: only who gets the slot is read.
        real = path_security.InlineThread.RUN_INLINE
        path_security.InlineThread.RUN_INLINE = ()
        self.addCleanup(setattr, path_security.InlineThread, "RUN_INLINE", real)
        config.rar_inprogress = False
        self.addCleanup(setattr, config, "rar_inprogress", False)

    def ask_for_the_list(self, user):
        self.request(self.list_name, user=user)

    def sending(self, user):
        """What the last send started for `user` was handed as its row."""
        for name, args in reversed(feed.InlineThread.dispatched):
            if name == "start_dcc_send" and args[1] == user:
                return args[5]
        raise AssertionError(f"nothing was started for {user}")

    def finish(self, user):
        """What start_dcc_send's finally does for a delivered send: settle the
        row by identity, free the slot and the nick, and put the nick at the
        back - unless it was a list."""
        row = self.sending(user)
        with mock.patch("builtins.print"):
            dcc.release_queue_entry(user, row, delivered=True)
        config.active_transfers[:] = [tx for tx in config.active_transfers if tx["user"] != user]
        config.user_processing_lock.discard(user)
        dcc.go_to_the_back(user, keep_place=dcc.is_a_list_row(row))

    def lists_that_went_first(self):
        return [tx["user"] for tx in config.active_transfers if tx.get("list_went_first")]

    def running_lists(self):
        return [tx["user"] for tx in config.active_transfers if tx["file"] == self.list_name]

    def busy_with_files(self, *users):
        """Every slot taken by a file send already running."""
        self.set_config(MAX_DCC_SLOTS=len(users))
        for user in users:
            self.request("A.flac", user=user)
        self.assertEqual(self.running(), list(users))

    def notices_to(self, user):
        return [message for to, message, *_ in self.oserve.queued if to == user]


class AListTakesTheFirstFreeSlot(Case):
    def setUp(self):
        super().setUp()
        self.busy_with_files("dave")
        self.request("C.flac", user="erin")
        self.request("D.flac", user="frank")

    def test_with_every_slot_busy_it_waits_and_interrupts_nothing(self):
        self.ask_for_the_list("gina")
        self.assertEqual(self.running(), ["dave"])
        self.assertEqual(self.queued("gina"), [self.list_name])

    def test_the_freed_slot_goes_to_the_list_before_the_queued_files(self):
        self.ask_for_the_list("gina")
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.running(), ["gina"])
        self.assertEqual(self.started()[-1], ("gina", self.list_name))

    def test_also_when_the_finishing_nick_has_a_file_of_its_own_next(self):
        self.request("B.flac", user="dave")
        self.ask_for_the_list("gina")
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.running(), ["gina"])

    def test_also_ahead_of_a_finishing_nick_that_has_waited_longer(self):
        """The trigger's own nick is passed over for a newer list, not only
        for a nick that waited longer than it."""
        self.finish("dave")
        config.dcc_queue.clear()
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="B.flac")]
        config.dcc_queue["gina"] = [queue_row(user="gina", filename=self.list_name, **{dcc.LIST_ROW_KEY: True})]
        runtime.queue_waiting_since.update({"dave": 1.0, "gina": 50.0})
        self.pick_up("dave")
        self.assertEqual(self.running(), ["gina"])
        self.assertEqual(self.lists_that_went_first(), ["gina"])

    def test_also_when_the_sweep_is_what_wakes(self):
        self.ask_for_the_list("gina")
        self.finish("dave")
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.running(), ["gina"])

    def test_and_the_files_then_go_in_their_order(self):
        self.ask_for_the_list("gina")
        order = []
        for finished in ("dave", "gina", "erin"):
            self.finish(finished)
            self.pick_up(finished)
            order.extend(self.running())
        self.assertEqual(order, ["gina", "erin", "frank"])

    def test_it_counts_against_the_cap_because_it_passed_nicks_that_waited(self):
        self.ask_for_the_list("gina")
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.lists_that_went_first(), ["gina"])

    def test_the_notice_says_the_list_is_next(self):
        self.ask_for_the_list("gina")
        told = "".join(self.notices_to("gina"))
        self.assertIn("Your list is next: " + self.list_name + " will be sent when a slot frees", told)
        self.assertNotIn("personal queue at position", told)

    def test_a_file_still_gets_the_queue_notice(self):
        self.request("E.flac", user="hank")
        self.assertIn("personal queue at position #1", "".join(self.notices_to("hank")))


class AListGoesToTheFrontOfItsNicksQueue(Case):
    def setUp(self):
        super().setUp()
        self.busy_with_files("dave")
        self.request("C.flac", user="erin")
        self.request("D.flac", user="erin")

    def test_ahead_of_the_nicks_own_files(self):
        self.ask_for_the_list("erin")
        self.assertEqual(self.queued("erin"), [self.list_name, "C.flac", "D.flac"])

    def test_the_wait_the_nick_already_had_is_kept(self):
        before = runtime.queue_waiting_since["erin"]
        self.ask_for_the_list("erin")
        self.assertEqual(runtime.queue_waiting_since["erin"], before)

    def test_a_list_is_not_the_nicks_turn(self):
        """erin waited longest: her list goes, and then her file still comes
        before frank's, who queued after her."""
        self.request("E.flac", user="frank")
        self.ask_for_the_list("erin")
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.started()[-1], ("erin", self.list_name))
        self.assertEqual(self.lists_that_went_first(), [], "erin had waited longest: the slot was hers anyway")
        self.finish("erin")
        self.pick_up("erin")
        self.assertEqual(self.started()[-1], ("erin", "C.flac"))


class OneSendPerNickStill(Case):
    def test_a_nick_downloading_a_file_gets_its_list_after_it_not_beside_it(self):
        self.set_config(MAX_DCC_SLOTS=2)
        self.request("A.flac", user="gina")
        self.ask_for_the_list("gina")
        self.assertEqual(self.running(), ["gina"])
        self.assertEqual(self.queued("gina"), [self.list_name])
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.running(), ["gina"], "a second slot is free, but not for gina's second send")

    def test_and_it_comes_next_once_that_file_is_done(self):
        self.busy_with_files("gina")
        self.request("C.flac", user="erin")
        self.ask_for_the_list("gina")
        self.finish("gina")
        self.pick_up("gina")
        self.assertEqual(self.started()[-1], ("gina", self.list_name))
        self.assertEqual(self.lists_that_went_first(), ["gina"], "erin had waited longer")

    def test_with_nobody_else_waiting_it_comes_next_and_passes_nobody(self):
        self.busy_with_files("gina")
        self.ask_for_the_list("gina")
        self.finish("gina")
        self.pick_up("gina")
        self.assertEqual(self.started()[-1], ("gina", self.list_name))
        self.assertEqual(self.lists_that_went_first(), [])


class TwoListsOfOneNick(Case):
    """Two list archives can both be asked for by name (a second dated one,
    or another list's): the second waits behind the first, both ahead of
    the nick's files."""

    def test_the_second_queues_behind_the_first_and_ahead_of_the_files(self):
        second = f"{config.LIST_BASE_NAME}-2026-09-02.zip"
        with io.open(os.path.join(self.tree.lists, second), "wb") as handle:
            handle.write(b"\x00" * 2048)
        self.busy_with_files("dave")
        self.request("C.flac", user="erin")
        self.ask_for_the_list("erin")
        self.assertEqual(self.last("QUEUED")["pos"], 1)
        self.request(second, user="erin")
        self.assertEqual(self.queued("erin"), [self.list_name, second, "C.flac"])
        self.assertEqual(self.last("QUEUED")["pos"], 2)


class ASecondListWaits(Case):
    def test_the_same_list_asked_for_again_while_it_goes_out_is_not_added(self):
        self.ask_for_the_list("gina")
        self.assertEqual(self.running(), ["gina"])
        self.ask_for_the_list("gina")
        self.assertEqual(self.queued("gina"), [])
        self.assertEqual(len([n for n, args in feed.InlineThread.dispatched if n == "start_dcc_send"]), 1)
        self.assertIn("is already being sent to you", "".join(self.notices_to("gina")))

    def test_the_same_list_asked_for_again_while_it_waits_is_not_added(self):
        self.busy_with_files("dave")
        self.ask_for_the_list("gina")
        self.ask_for_the_list("gina")
        self.assertEqual(self.queued("gina"), [self.list_name])

    def test_another_nicks_list_waits_while_one_that_went_first_is_out(self):
        self.busy_with_files("dave", "hank")
        self.request("C.flac", user="erin")
        self.request("D.flac", user="frank")
        self.ask_for_the_list("gina")
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.lists_that_went_first(), ["gina"])
        self.ask_for_the_list("ivan")
        self.finish("hank")
        self.pick_up("hank")
        self.assertEqual(self.started()[-1], ("erin", "C.flac"), "erin waited longest, and a list is already out")
        self.finish("gina")
        self.pick_up("gina")
        self.assertEqual(self.started()[-1], ("ivan", self.list_name), "the cap is free again: ivan's list goes first")

    def test_a_list_asked_for_with_a_slot_free_and_one_out_does_not_pass_the_waiting(self):
        """The direct path obeys the cap too: with a slot free for a moment
        (the trigger has not run yet) and a list already out, a new list does
        not take it from erin."""
        self.set_config(MAX_DCC_SLOTS=2)
        config.active_transfers.append({"user": "kate", "file": self.list_name, "bytes_sent": 0,
                                        "list_went_first": True})
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        runtime.queue_waiting_since["erin"] = 1.0
        self.ask_for_the_list("gina")
        self.assertEqual(self.queued("gina"), [self.list_name])
        self.assertEqual(self.running(), ["kate"])

    def test_without_one_out_the_same_list_takes_that_slot(self):
        self.set_config(MAX_DCC_SLOTS=2)
        config.active_transfers.append({"user": "kate", "file": "X.flac", "bytes_sent": 0})
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        runtime.queue_waiting_since["erin"] = 1.0
        self.ask_for_the_list("gina")
        self.assertEqual(self.running(), ["kate", "gina"])
        self.assertEqual(self.lists_that_went_first(), ["gina"])

    def test_a_nick_with_files_waiting_and_a_slot_free_gets_its_list_at_once(self):
        self.set_config(MAX_DCC_SLOTS=2)
        config.active_transfers.append({"user": "kate", "file": "X.flac", "bytes_sent": 0})
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="C.flac")]
        runtime.queue_waiting_since["erin"] = 1.0
        self.ask_for_the_list("erin")
        self.assertEqual(self.running(), ["kate", "erin"])
        self.assertEqual(self.queued("erin"), ["C.flac"])


class TheCapHoldsUnderABurst(Case):
    FILE_NICKS = ("dave", "erin", "frank")
    WAITING = ("gina", "hank", "ivan")
    LISTS = ("judy", "kate", "liam", "mona")

    def setUp(self):
        super().setUp()
        self.busy_with_files(*self.FILE_NICKS)
        for nick in self.WAITING:
            self.request("C.flac", user=nick)

    def test_queued_lists_go_out_one_at_a_time_beyond_the_files(self):
        for nick in self.LISTS:
            self.ask_for_the_list(nick)
        already = len(self.started())
        for _ in range(12):
            if not config.active_transfers:
                break
            done = config.active_transfers[0]["user"]
            self.finish(done)
            self.pick_up(done)
            self.assertLessEqual(len(self.lists_that_went_first()), 1)
        served = [user for user, _name in self.started()[already:]]
        # One list ahead, then the files and the lists by turns: the files
        # are never starved, and every list still goes.
        self.assertEqual(served, ["judy", "gina", "hank", "kate", "ivan", "liam", "mona"])

    def test_a_burst_of_list_requests_into_free_slots_takes_one_ahead_of_the_waiting(self):
        """Every slot free for a moment, three nicks waiting for files: the
        first list takes one, the rest queue, and the files keep two slots."""
        config.active_transfers.clear()
        config.user_processing_lock.clear()
        for nick in self.LISTS:
            self.ask_for_the_list(nick)
        self.assertEqual(self.running_lists(), ["judy"])
        self.assertEqual(self.lists_that_went_first(), ["judy"])
        for nick in self.LISTS[1:]:
            self.assertEqual(self.queued(nick), [self.list_name])

    def test_a_concurrent_trigger_that_sent_a_list_meanwhile_is_honoured(self):
        """The rank is taken under one hold of the lock and the claim under
        another: a list that went first in between is seen at the claim, and
        the slot goes to the nick that waited longest instead."""
        self.set_config(MAX_DCC_SLOTS=4)
        config.dcc_queue["judy"] = [queue_row(user="judy", filename=self.list_name, **{dcc.LIST_ROW_KEY: True})]
        runtime.queue_waiting_since["judy"] = runtime.queue_waiting_since["ivan"] + 1
        real = dcc.announce_channel_for

        def another_list_goes_first_meanwhile(row):
            if not any(tx["user"] == "kate" for tx in config.active_transfers):
                config.active_transfers.append({"user": "kate", "file": self.list_name, "bytes_sent": 0,
                                                "list_went_first": True})
            return real(row)

        config.active_transfers.pop()       # one slot free for judy's trigger
        with mock.patch.object(dcc, "announce_channel_for", another_list_goes_first_meanwhile):
            self.pick_up("judy")
        self.assertEqual(self.lists_that_went_first(), ["kate"])
        self.assertNotIn("judy", self.running())
        self.assertEqual(self.started()[-1], ("gina", "C.flac"))


class TheOtherRulesStand(Case):
    def setUp(self):
        super().setUp()
        self.busy_with_files("dave")
        self.request("C.flac", user="erin")

    def test_a_frozen_nicks_list_does_not_go_first(self):
        self.ask_for_the_list("gina")
        config.frozen_queues["gina"] = 1
        # gina is still in the channel list, so the freeze sweep would thaw
        # her; held off, so it is the frozen queue the dispatcher meets.
        self.set_config(bot_joined_channel=False)
        self.finish("dave")
        self.pick_up("dave")
        self.assertEqual(self.running(), ["erin"])

    def test_an_absent_nicks_list_is_frozen_like_any_queue(self):
        self.ask_for_the_list("gina")
        config.channel_users[feed.OTHER].discard("gina")
        self.finish("dave")
        with mock.patch.object(dcc, "freeze_absent_user") as freeze:
            self.pick_up("system_next_trigger_fallback")
        self.assertEqual([c.args[1] for c in freeze.call_args_list], ["gina"])
        self.assertEqual(self.running(), ["erin"])

    def test_a_list_goes_before_a_folder_pack_that_waited_and_the_pack_follows(self):
        config.dcc_queue["frank"] = [queue_row(user="frank", filename="Album.rar",
                                               is_unpacked_rar_folder=True, is_temporary_zip=True)]
        runtime.queue_waiting_since["frank"] = 1.0
        self.ask_for_the_list("gina")
        self.finish("dave")
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.running(), ["gina"])
        woken = [args[-1] for name, args in feed.InlineThread.dispatched if name == "check_queue_and_send"]
        self.assertEqual(woken, [], "no pack is woken in the pass the list took")
        self.finish("gina")
        self.pick_up("system_next_trigger_fallback")
        woken = [args[-1] for name, args in feed.InlineThread.dispatched if name == "check_queue_and_send"]
        self.assertEqual(woken, ["frank"])

    def test_with_no_list_anywhere_the_order_is_the_wait_alone(self):
        config.dcc_queue["frank"] = [queue_row(user="frank", filename="D.flac")]
        config.dcc_queue["gina"] = [queue_row(user="gina", filename="E.flac")]
        runtime.queue_waiting_since.update({"erin": 30.0, "frank": 10.0, "gina": 20.0})
        self.finish("dave")
        self.pick_up("system_next_trigger_fallback")
        self.assertEqual(self.running(), ["frank"])


class AFileIsNotMarkedAsAList(Case):
    def test_a_file_sent_at_once_carries_no_list_marks(self):
        self.request("A.flac", user="dave")
        row = self.sending("dave")
        self.assertFalse(dcc.is_a_list_row(row))
        self.assertNotIn("list_went_first", config.active_transfers[0])

    def test_a_list_sent_at_once_is_marked_on_its_row(self):
        self.ask_for_the_list("dave")
        self.assertTrue(dcc.is_a_list_row(self.sending("dave")))
        self.assertEqual(self.lists_that_went_first(), [], "nobody was waiting: the slot was dave's anyway")


class TheSendItselfKeepsThePlace(Case):
    """start_dcc_send's own exits, not the stand-in above: one that needs no
    socket runs everywhere, and a real send over loopback below."""

    def run_without_a_connection(self, row):
        nick = "erin"
        config.dcc_queue[nick] = [queue_row(user=nick, filename="C.flac")]
        runtime.queue_waiting_since[nick] = 5.0
        config.user_processing_lock.add(nick)
        config.active_transfers.append({"user": nick, "file": row["file"], "bytes_sent": 0})
        self.oserve.irc_connection = None
        with mock.patch("builtins.print"):
            dcc.start_dcc_send(self.sock, nick, row["path"], row["file"], feed.OTHER, row)
        return runtime.queue_waiting_since[nick]

    def test_a_list_leaves_the_nicks_wait_alone(self):
        row = {"path": os.path.join(self.tree.lists, self.list_name), "file": self.list_name,
               dcc.LIST_ROW_KEY: True}
        self.assertEqual(self.run_without_a_connection(row), 5.0)

    def test_a_file_puts_the_nick_at_the_back(self):
        row = {"path": os.path.join(self.tree.music, "A.flac"), "file": "A.flac"}
        self.assertGreater(self.run_without_a_connection(row), 5.0)


@unittest.skipUnless(renamed.loopback_is_usable(), "needs a loopback socket")
class ARealListSendKeepsThePlace(ack.ARealReceiver):
    # ARealReceiver carries its own tests; only its receiver plumbing is wanted.
    def send_and_receive(self, row):
        nick = ack.USER.lower()
        config.channel_users = {"#somechannel": {ack.USER}}
        config.user_processing_lock = {nick}
        config.dcc_queue[nick] = [queue_row(user=ack.USER, filename="Next.flac")]
        config.frozen_queues[nick] = 0          # the wake must not start the next row
        runtime.queue_waiting_since[nick] = 5.0
        irc = ack.RecordingIrcSocket()
        self.oserve.irc_connection = irc
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, ack.USER, self.served, "Some_Album.zip", "#somechannel", row),
            daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc.handshake_seen.wait(20), "no DCC SEND handshake")
        self.receive(self.connect(irc.port()))
        sender.join(30)
        self.assertFalse(sender.is_alive(), "the send did not finish")
        self.assertNotIn(nick, config.user_processing_lock)
        return runtime.queue_waiting_since[nick]

    def test_a_list_leaves_the_wait_alone(self):
        row = {"path": self.served, "file": "Some_Album.zip", dcc.LIST_ROW_KEY: True}
        self.assertEqual(self.send_and_receive(row), 5.0)

    def test_a_file_moves_it(self):
        row = {"path": self.served, "file": "Some_Album.zip"}
        self.assertGreater(self.send_and_receive(row), 5.0)


for _name in [n for n in dir(ack.ARealReceiver) if n.startswith("test")]:
    setattr(ARealListSendKeepsThePlace, _name, None)



class TheQueueIsShownInTheOrderSlotsGoOut(unittest.TestCase):
    """commands.queue_order() - the Queue page, `queue` and what move up/down
    moves against (#1206) - ranks with the dispatcher's key (#1205)."""

    def setUp(self):
        self._saved_queue = config.dcc_queue
        self._saved_stamps = dict(runtime.queue_waiting_since)
        self._saved_transfers = config.active_transfers
        config.dcc_queue = {
            "old": [{"file": "a.mp3", "path": "/x/a.mp3"}],
            "newer": [{"file": "list.zip", "path": "/x/list.zip", dcc.LIST_ROW_KEY: True}],
        }
        runtime.queue_waiting_since.clear()
        runtime.queue_waiting_since.update({"old": 100.0, "newer": 200.0})
        config.active_transfers = []

    def tearDown(self):
        config.dcc_queue = self._saved_queue
        runtime.queue_waiting_since.clear()
        runtime.queue_waiting_since.update(self._saved_stamps)
        config.active_transfers = self._saved_transfers

    def test_a_waiting_list_is_shown_first(self):
        self.assertEqual(commands.queue_order(), ["newer", "old"])

    def test_while_a_list_that_went_first_is_out_the_wait_decides(self):
        config.active_transfers = [{"user": "someone", "list_went_first": True}]
        self.assertEqual(commands.queue_order(), ["old", "newer"])

    def test_with_no_list_waiting_the_wait_decides(self):
        config.dcc_queue["newer"] = [{"file": "b.mp3", "path": "/x/b.mp3"}]
        self.assertEqual(commands.queue_order(), ["old", "newer"])

if __name__ == "__main__":
    unittest.main()
