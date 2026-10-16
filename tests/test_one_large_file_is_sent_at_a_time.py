"""At most one large file is sent at a time (#1387).

With MAX_DCC_SLOTS = 3 and three nicks each asking for a multi-gigabyte
archive, every slot was held for hours and every ordinary track waited behind
them. LARGE_FILE_MB (0 = off) makes a file of at least that many MB large, and
while a large file is being sent a nick whose next file is large is passed
over, keeping its place: the slot goes to the next in line.

Driven through the real dispatcher: claim_next_in_line() (the sweep), section
A of check_queue_and_send() (the nick that just finished), and
handle_download_request() (a request that would have been sent at once). The
size a row is judged by is recorded on it when it is queued; a row without
one is small.
"""

import contextlib
import io
import os
import threading
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import runtime  # noqa: E402
import web_queue  # noqa: E402

import tests.test_the_longest_waiting_nick_gets_the_slot as fair  # noqa: E402
from tests import test_the_feed_says_which_channel as feed  # noqa: E402

MB = 1024 * 1024
OTHER = feed.OTHER
SWEEP = feed.SWEEP
BIG = "Big One.flac"            # exactly 1 MiB: LARGE_FILE_MB = 1 makes it large
BIG_TWO = "Big Two.flac"
SMALL = "A.flac"                # 4096 bytes, made by fair.Case
SMALL_TWO = "C.flac"
LARGE_LINE = "next file is large"
# Somebody else's large send, already running: its real size is known.
RUNNING_BIG = {"user": "gina", "file": "Huge Archive.flac", "bytes_sent": 0, "size": 5 * MB}
RUNNING_SMALL = {"user": "hank", "file": "Little.flac", "bytes_sent": 0, "size": 4096}


class RecordedThread(feed.InlineThread):
    """InlineThread that also keeps every thread it was asked for, its
    kwargs included, so a test can run one as its thread would."""

    made = []

    def start(self):
        RecordedThread.made.append(self)
        super().start()


def _pristine(module, name):
    """The daemon's own function, from before any test stubbed it."""
    for owner, attr, original in support._PRISTINE:
        if owner is module and attr == name:
            return original
    raise LookupError(name)


class Case(fair.Case):
    """fair.Case's library and channel, two 1 MiB files beside its tracks,
    three slots and the rule on at 1 MB unless a test says otherwise."""

    def setUp(self):
        super().setUp()
        for name in (BIG, BIG_TWO):
            with io.open(os.path.join(self.tree.music, name), "wb") as handle:
                handle.truncate(MB)
        self.set_config(MAX_DCC_SLOTS=3, LARGE_FILE_MB=1, rar_inprogress=False)
        config.channel_users[OTHER] = {"dave", "erin", "frank", "ivan"}
        RecordedThread.made = []
        dcc.threading.Thread = RecordedThread
        self.addCleanup(setattr, dcc.threading, "Thread", feed.InlineThread)

    def row(self, user, name, size=None, **extra):
        """A queue row as handle_download_request() makes one; `size` is its
        recorded size, absent when None."""
        entry = {"file": name, "path": os.path.join(self.tree.music, name), "channel": OTHER,
                 "user_raw": user, "is_temporary_zip": False}
        if size is not None:
            entry[dcc.QUEUED_SIZE_KEY] = size
        entry.update(extra)
        return entry

    def queue(self, **rows_by_nick):
        """One row per nick, waiting in keyword order (the first waited longest)."""
        for place, (nick, entry) in enumerate(rows_by_nick.items(), start=1):
            config.dcc_queue[nick] = [entry]
            runtime.queue_waiting_since[nick] = float(place)

    def running(self, *transfers):
        config.active_transfers.extend(dict(tx) for tx in transfers)

    def claim(self):
        """claim_next_in_line(), its log kept: (promoted_key, log)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _promoted, key, _pack, _absent = dcc.claim_next_in_line()
        return key, out.getvalue()

    def logged(self, call, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            call(self.sock, *args)
        return out.getvalue()

    def sweeps_started(self):
        """The sweeps dispatches started on threads, in order."""
        return [t for t in RecordedThread.made
                if getattr(t.target, "__name__", "") == "check_queue_and_send" and t.args[1] == SWEEP]

    def sends_started(self):
        return [t.args[1] for t in RecordedThread.made
                if getattr(t.target, "__name__", "") == "start_dcc_send"]

    def run_the_threads(self, release_sends=False):
        """Run each sweep a dispatch started, as its thread would (its kwargs
        too), in order, until none is left. With release_sends, every send
        started hands its claim straight back, as a send refused at once
        does. Returns how many sweeps ran."""
        ran = 0
        index = 0
        while index < len(RecordedThread.made):
            self.assertLess(index, 50, "the passes did not stop")
            thread = RecordedThread.made[index]
            index += 1
            name = getattr(thread.target, "__name__", "")
            if name == "check_queue_and_send":
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    thread.target(*thread.args, **thread.kwargs)
                ran += 1
            elif name == "start_dcc_send" and release_sends:
                nick = str(thread.args[1]).lower()
                config.active_transfers[:] = [tx for tx in config.active_transfers
                                              if str(tx.get("user", "")).lower() != nick]
                config.user_processing_lock.discard(nick)
        return ran


class TheSweepPassesALargeHeadOver(Case):

    def setUp(self):
        super().setUp()
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))

    def test_off_it_is_the_order_of_today_with_a_large_send_running(self):
        self.set_config(LARGE_FILE_MB=0)
        self.running(RUNNING_BIG)
        key, log = self.claim()
        self.assertEqual(key, "dave")
        self.assertNotIn(LARGE_LINE, log)

    def test_on_the_large_head_waits_and_the_small_one_behind_it_is_claimed(self):
        self.running(RUNNING_BIG)
        key, log = self.claim()
        self.assertEqual(key, "erin")
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["gina", "erin"])
        # Its place is kept: still first in its queue, its wait not restarted.
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]], [BIG])
        self.assertEqual(runtime.queue_waiting_since["dave"], 1.0)
        self.assertIn("[DCC QUEUE] dave's next file is large (1 MB) and another large file is being sent"
                      " - the slot goes to the next in line.", log)

    def test_with_no_large_send_running_the_large_head_goes_first(self):
        self.running(RUNNING_SMALL)
        key, log = self.claim()
        self.assertEqual(key, "dave")
        self.assertNotIn(LARGE_LINE, log)

    def test_once_the_large_send_ends_the_large_head_is_next(self):
        self.running(RUNNING_BIG)
        self.assertEqual(self.claim()[0], "erin")
        config.active_transfers[:] = [tx for tx in config.active_transfers if tx["user"] != "gina"]
        self.assertEqual(self.claim()[0], "dave")

    def test_its_fresh_claim_counts_as_large_before_the_send_reads_a_size(self):
        """The claim the sweep made has no "size" yet: its queue row's
        recorded size is what a concurrent dispatch counts."""
        config.dcc_queue["frank"] = [self.row("frank", BIG_TWO, MB)]
        runtime.queue_waiting_since["frank"] = 0.5
        self.assertEqual(self.claim()[0], "frank")
        (tx,) = config.active_transfers
        self.assertNotIn("size", tx)
        self.assertTrue(dcc.waits_for_a_large_send(config.dcc_queue["dave"]))
        key, log = self.claim()
        self.assertEqual(key, "erin")
        self.assertIn("dave's " + LARGE_LINE, log)

    def test_a_running_send_of_exactly_the_limit_is_large(self):
        self.running(dict(RUNNING_BIG, size=MB))
        self.assertEqual(self.claim()[0], "erin")

    def test_a_running_send_one_byte_under_the_limit_is_not(self):
        self.running(dict(RUNNING_BIG, size=MB - 1))
        self.assertEqual(self.claim()[0], "dave")


class WhatCountsAsLarge(Case):

    def test_a_row_of_exactly_the_limit_is_large(self):
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))
        self.running(RUNNING_BIG)
        self.assertEqual(self.claim()[0], "erin")

    def test_a_row_one_byte_under_the_limit_is_not(self):
        self.queue(dave=self.row("dave", BIG, MB - 1), erin=self.row("erin", SMALL, 4096))
        self.running(RUNNING_BIG)
        self.assertEqual(self.claim()[0], "dave")

    def test_a_row_without_a_recorded_size_is_small(self):
        """A row from before #1387, or a folder pack's: a guess never holds a file back."""
        self.queue(dave=self.row("dave", BIG), erin=self.row("erin", SMALL, 4096))
        self.running(RUNNING_BIG)
        self.assertEqual(self.claim()[0], "dave")

    def test_a_recorded_size_a_hand_edit_broke_is_small(self):
        for junk in ("lots", -5 * MB, [MB], None):
            with self.subTest(queued_size=junk):
                config.dcc_queue.clear()
                config.active_transfers.clear()
                config.user_processing_lock.clear()
                self.queue(dave=self.row("dave", BIG, junk), erin=self.row("erin", SMALL, 4096))
                self.running(RUNNING_BIG)
                self.assertEqual(self.claim()[0], "dave")

    def test_an_unusable_setting_is_off(self):
        for junk in ("junk", -3, None, "1.5"):
            with self.subTest(LARGE_FILE_MB=junk):
                self.set_config(LARGE_FILE_MB=junk)
                config.dcc_queue.clear()
                config.active_transfers.clear()
                config.user_processing_lock.clear()
                self.queue(dave=self.row("dave", BIG, 5 * MB), erin=self.row("erin", SMALL, 4096))
                self.running(RUNNING_BIG)
                key, log = self.claim()
                self.assertEqual(key, "dave")
                self.assertNotIn(LARGE_LINE, log)

    def test_the_setting_counts_in_mebibytes(self):
        self.set_config(LARGE_FILE_MB=2)
        self.queue(dave=self.row("dave", BIG, 2 * MB - 1), erin=self.row("erin", SMALL, 4096))
        self.running(dict(RUNNING_BIG, size=2 * MB))
        self.assertEqual(self.claim()[0], "dave")
        config.dcc_queue["dave"][0][dcc.QUEUED_SIZE_KEY] = 2 * MB
        config.active_transfers[:] = [dict(RUNNING_BIG, size=2 * MB)]
        config.user_processing_lock.clear()
        self.assertEqual(self.claim()[0], "erin")


class TheListIsNeverLarge(Case):

    def setUp(self):
        super().setUp()
        self.list_name = f"{config.LIST_BASE_NAME}-2026-09-01.zip"
        self.assertTrue(list_mod.is_list_artifact_name(self.list_name))

    def test_a_send_of_the_list_holds_no_large_file_back(self):
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))
        self.running(dict(RUNNING_BIG, file=self.list_name))
        self.assertEqual(self.claim()[0], "dave")

    def test_nor_does_a_claim_whose_row_is_the_list(self):
        list_row = self.row("gina", self.list_name, 5 * MB, **{dcc.LIST_ROW_KEY: True})
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))
        self.running({"user": "gina", "file": "renamed.bin", "bytes_sent": 0, "queue_row": list_row})
        self.assertEqual(self.claim()[0], "dave")

    def test_a_list_row_waits_for_no_large_send(self):
        self.queue(dave=self.row("dave", self.list_name, 5 * MB, **{dcc.LIST_ROW_KEY: True}))
        self.running(RUNNING_BIG)
        key, log = self.claim()
        self.assertEqual(key, "dave")
        self.assertNotIn(LARGE_LINE, log)


class TheNickThatJustFinished(Case):
    """Section A of check_queue_and_send(): the finished nick's own next file
    is large while another large file is being sent."""

    def setUp(self):
        super().setUp()
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))

    def started(self):
        return [(args[1], args[3]) for name, args in feed.InlineThread.dispatched if name == "start_dcc_send"]

    def test_off_their_large_file_is_sent(self):
        self.set_config(LARGE_FILE_MB=0)
        self.running(RUNNING_BIG)
        self.logged(dcc.check_queue_and_send, "dave")
        self.assertEqual(self.started(), [("dave", BIG)])

    def test_on_it_waits_and_the_slot_goes_to_the_sweep(self):
        self.running(RUNNING_BIG)
        log = self.logged(dcc.check_queue_and_send, "dave")
        self.assertEqual(self.started(), [("erin", SMALL)])
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["gina", "erin"])
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]], [BIG])
        # One line for the nick passed over, not one from each path.
        self.assertEqual(log.count("dave's " + LARGE_LINE), 1)

    def test_with_no_large_send_running_their_file_is_sent(self):
        self.running(RUNNING_SMALL)
        self.logged(dcc.check_queue_and_send, "dave")
        self.assertEqual(self.started(), [("dave", BIG)])


class ANewRequest(Case):
    """handle_download_request() records the size and keeps a large file
    out of a free slot while another large file is being sent."""

    def ask(self, what, user):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, user, what, OTHER)
        return out.getvalue()

    def test_the_size_on_disk_is_recorded_when_it_is_queued(self):
        self.running(RUNNING_SMALL, dict(RUNNING_SMALL, user="ivan"), dict(RUNNING_SMALL, user="judy"))
        self.ask(SMALL, "dave")
        (entry,) = config.dcc_queue["dave"]
        self.assertEqual(entry[dcc.QUEUED_SIZE_KEY], os.path.getsize(os.path.join(self.tree.music, SMALL)))
        self.assertEqual(entry[dcc.QUEUED_SIZE_KEY], 4096)

    def test_and_survives_the_queue_file(self):
        self.running(RUNNING_SMALL, dict(RUNNING_SMALL, user="ivan"), dict(RUNNING_SMALL, user="judy"))
        self.ask(BIG, "dave")
        path = os.path.join(self.tree.root, "dcc_queue.txt")
        with mock.patch.object(db, "DCC_QUEUE_FILE", path), contextlib.redirect_stdout(io.StringIO()):
            _pristine(db, "save_dcc_queue")()
            config.dcc_queue.clear()
            db.load_dcc_queue()
        (entry,) = config.dcc_queue["dave"]
        self.assertEqual(entry[dcc.QUEUED_SIZE_KEY], MB)
        self.running(RUNNING_BIG)
        self.assertTrue(dcc.waits_for_a_large_send(config.dcc_queue["dave"]))

    def test_the_list_gets_no_size(self):
        list_name = f"{config.LIST_BASE_NAME}-2026-09-01.zip"
        with io.open(os.path.join(self.tree.lists, list_name), "wb") as handle:
            handle.write(b"\x00" * 2048)
        self.running(RUNNING_SMALL, dict(RUNNING_SMALL, user="ivan"), dict(RUNNING_SMALL, user="judy"))
        self.ask(list_name, "dave")
        (entry,) = config.dcc_queue["dave"]
        self.assertEqual(entry["file"], list_name)
        self.assertNotIn(dcc.QUEUED_SIZE_KEY, entry)

    def test_a_large_file_is_queued_not_sent_while_another_is_sent(self):
        self.running(RUNNING_BIG)
        log = self.ask(BIG, "dave")
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["gina"])
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]], [BIG])
        self.assertIn("dave's " + LARGE_LINE + " (1 MB)", log)

    def test_off_it_is_sent_at_once(self):
        self.set_config(LARGE_FILE_MB=0)
        self.running(RUNNING_BIG)
        self.ask(BIG, "dave")
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["gina", "dave"])

    def test_a_small_file_is_still_sent_at_once(self):
        self.running(RUNNING_BIG)
        self.ask(SMALL, "dave")
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["gina", "dave"])

    def test_one_sent_at_once_counts_as_large_from_its_claim_on(self):
        """No queue row carries its size: the claim does, before the send
        reads the real one."""
        self.ask(BIG, "dave")
        (claim,) = config.active_transfers
        self.assertEqual(claim["user"], "dave")
        self.assertNotIn("size", claim)
        self.assertEqual(claim[dcc.QUEUED_SIZE_KEY], MB)
        self.ask(BIG_TWO, "erin")
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["dave"])
        self.assertEqual([r["file"] for r in config.dcc_queue["erin"]], [BIG_TWO])


class AHeldBackNickKeepsNoSlotFree(Case):
    """nicks_waiting_for_a_slot() leaves out a nick that could not take a
    slot now, so a newcomer's small file still goes straight out."""

    def setUp(self):
        super().setUp()
        self.queue(dave=self.row("dave", BIG, MB))
        self.running(RUNNING_BIG, RUNNING_SMALL)

    def test_it_is_not_counted_as_waiting(self):
        self.assertEqual(dcc.nicks_waiting_for_a_slot(), [])
        self.assertTrue(dcc.a_slot_is_free_beyond_those_waiting("frank"))

    def test_until_the_large_send_ends(self):
        config.active_transfers[:] = [dict(RUNNING_SMALL), dict(RUNNING_SMALL, user="ivan")]
        self.assertEqual(dcc.nicks_waiting_for_a_slot(), ["dave"])
        self.assertFalse(dcc.a_slot_is_free_beyond_those_waiting("frank"))

    def test_a_newcomers_small_file_goes_at_once(self):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.handle_download_request(self.sock, "frank", SMALL, OTHER)
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["gina", "hank", "frank"])
        self.assertNotIn("frank", config.dcc_queue)

    def test_off_the_nick_is_waited_for_as_before(self):
        self.set_config(LARGE_FILE_MB=0)
        self.assertEqual(dcc.nicks_waiting_for_a_slot(), ["dave"])
        self.assertFalse(dcc.a_slot_is_free_beyond_those_waiting("frank"))


class TheSlotsAHeldFileLeftAreFilled(Case):
    """A trigger dispatches one nick. With the rule on, a dispatch that
    leaves a slot free starts one more pass of the sweep; a pass that claims
    nobody starts none."""

    def setUp(self):
        super().setUp()
        self.set_config(MAX_DCC_SLOTS=4)
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096),
                   frank=self.row("frank", SMALL_TWO, 4096))
        self.running(RUNNING_BIG)

    def senders(self):
        return [tx["user"] for tx in config.active_transfers]

    def test_on_a_second_nick_is_claimed_without_another_trigger(self):
        self.logged(dcc.check_queue_and_send, SWEEP)
        self.assertEqual(self.senders(), ["gina", "erin"])
        self.assertEqual([t.kwargs for t in self.sweeps_started()], [{"fill_pass": True}])
        ran = self.run_the_threads()
        self.assertEqual(self.senders(), ["gina", "erin", "frank"])
        # The fill pass claimed frank and started no pass of its own, though
        # a slot is still free and dave still waits.
        self.assertEqual(ran, 1)
        self.assertEqual(len(self.sweeps_started()), 1)
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]], [BIG])

    def test_from_section_a_too(self):
        """erin's own next file is small; the slot it leaves is filled
        because dave, ahead of her, is held back."""
        log = self.logged(dcc.check_queue_and_send, "erin")
        self.assertEqual(self.senders(), ["gina", "erin"])
        self.assertNotIn(LARGE_LINE, log)
        self.assertEqual([t.kwargs for t in self.sweeps_started()], [{"fill_pass": True}])
        self.run_the_threads()
        self.assertEqual(self.senders(), ["gina", "erin", "frank"])

    def test_off_no_extra_pass_is_started(self):
        self.set_config(LARGE_FILE_MB=0)
        self.logged(dcc.check_queue_and_send, SWEEP)
        self.assertEqual(self.senders(), ["gina", "dave"])
        self.assertEqual(self.sweeps_started(), [])

    def test_with_every_slot_taken_no_extra_pass_is_started(self):
        self.set_config(MAX_DCC_SLOTS=2)
        self.logged(dcc.check_queue_and_send, SWEEP)
        self.assertEqual(self.senders(), ["gina", "erin"])
        self.assertEqual(self.sweeps_started(), [])


class TheFillPassIsOneHop(Case):
    """A send refused at once hands its slot straight back. A pass after
    each of those would refuse the same nick over and over: one trigger
    dispatches at most twice with the rule on, once with it off."""

    def setUp(self):
        super().setUp()
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))
        self.running(RUNNING_BIG)

    def test_on_at_most_two_dispatches(self):
        self.logged(dcc.check_queue_and_send, SWEEP)
        self.run_the_threads(release_sends=True)
        self.assertEqual(self.sends_started(), ["erin", "erin"])
        self.assertEqual([t.kwargs for t in self.sweeps_started()], [{"fill_pass": True}])

    def test_off_one(self):
        self.set_config(LARGE_FILE_MB=0)
        self.logged(dcc.check_queue_and_send, SWEEP)
        self.run_the_threads(release_sends=True)
        self.assertEqual(self.sends_started(), ["dave"])
        self.assertEqual(self.sweeps_started(), [])

    def test_a_fill_pass_starts_no_further_sweep(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.check_queue_and_send(self.sock, SWEEP, fill_pass=True)
        self.assertEqual(self.sends_started(), ["erin"])
        self.assertIn("dave's " + LARGE_LINE, out.getvalue())
        self.assertEqual(self.sweeps_started(), [])

    def test_nor_does_one_named_for_a_nick(self):
        """No caller does this today - a fill pass is always the sweep - but
        the contract is the function's: a fill pass starts no further one."""
        config.dcc_queue["erin"] = [self.row("erin", SMALL, 4096)]
        runtime.queue_waiting_since["erin"] = 0.5
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.check_queue_and_send(self.sock, "erin", fill_pass=True)
        self.assertEqual(self.sends_started(), ["erin"])
        self.assertEqual(self.sweeps_started(), [])


class TheFillPassOnlyFollowsALargeFile(Case):
    """Only a pass that passed over a held large head, or dispatched a large
    file, starts the fill pass; a plain dispatch leaves a free slot to the
    next trigger, as before."""

    def test_a_small_dispatch_that_passed_nobody_over_starts_none(self):
        self.queue(erin=self.row("erin", SMALL, 4096), frank=self.row("frank", SMALL_TWO, 4096))
        self.running(RUNNING_BIG)
        self.logged(dcc.check_queue_and_send, SWEEP)
        self.assertEqual(self.sends_started(), ["erin"])
        self.assertEqual(len(config.active_transfers), 2)
        self.assertEqual(self.sweeps_started(), [])

    def test_nor_does_section_a(self):
        self.queue(erin=self.row("erin", SMALL, 4096), frank=self.row("frank", SMALL_TWO, 4096))
        self.logged(dcc.check_queue_and_send, "erin")
        self.assertEqual(self.sends_started(), ["erin"])
        self.assertEqual(self.sweeps_started(), [])

    def test_one_that_passed_a_held_head_over_starts_one(self):
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))
        self.running(RUNNING_BIG)
        self.logged(dcc.check_queue_and_send, SWEEP)
        self.assertEqual(self.sends_started(), ["erin"])
        self.assertEqual([t.kwargs for t in self.sweeps_started()], [{"fill_pass": True}])

    def test_one_that_dispatched_a_large_file_starts_one(self):
        """The large send just claimed holds the large head behind it back:
        the slot that leaves goes to the small one further on."""
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", BIG_TWO, MB),
                   frank=self.row("frank", SMALL, 4096))
        self.logged(dcc.check_queue_and_send, SWEEP)
        self.assertEqual(self.sends_started(), ["dave"])
        self.assertEqual([t.kwargs for t in self.sweeps_started()], [{"fill_pass": True}])
        self.run_the_threads()
        self.assertEqual(self.sends_started(), ["dave", "frank"])
        self.assertEqual([r["file"] for r in config.dcc_queue["erin"]], [BIG_TWO])

    def test_so_does_section_a_dispatching_a_large_file(self):
        self.queue(dave=self.row("dave", BIG, MB), frank=self.row("frank", SMALL, 4096))
        self.logged(dcc.check_queue_and_send, "dave")
        self.assertEqual(self.sends_started(), ["dave"])
        self.assertEqual([t.kwargs for t in self.sweeps_started()], [{"fill_pass": True}])


class ClaimNextInLineReportsWhoItPassedOver(Case):

    def setUp(self):
        super().setUp()
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))
        self.running(RUNNING_BIG)

    def test_the_held_nick_is_appended(self):
        passed = []
        with contextlib.redirect_stdout(io.StringIO()):
            _promoted, key, _pack, _absent = dcc.claim_next_in_line(passed_over=passed)
        self.assertEqual(key, "erin")
        self.assertEqual(passed, ["dave"])

    def test_nobody_passed_over_leaves_it_empty(self):
        config.active_transfers[:] = [dict(RUNNING_SMALL)]
        passed = []
        with contextlib.redirect_stdout(io.StringIO()):
            _promoted, key, _pack, _absent = dcc.claim_next_in_line(passed_over=passed)
        self.assertEqual(key, "dave")
        self.assertEqual(passed, [])

    def test_without_it_the_four_values_are_unchanged(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = dcc.claim_next_in_line()
        self.assertEqual(len(result), 4)
        promoted, key, pack, absent = result
        self.assertEqual(key, "erin")
        self.assertEqual(promoted[0], "erin")
        self.assertIsNone(pack)
        self.assertEqual(absent, [])


class ALargeTurnIsKeptForWhoWaitedLonger(Case):
    """No large file is being sent, a slot is idle, and erin, who has waited,
    has a large file next: a newcomer's large file is queued behind her, not
    sent in the turn that is hers."""

    WAITED_LINE = ("[DCC QUEUE] frank's file is large (1 MB) and erin has waited longer with a large file"
                   " next - it is queued behind them.")

    def setUp(self):
        super().setUp()
        self.queue(erin=self.row("erin", BIG, MB))
        self.running(RUNNING_SMALL)

    def ask(self, what, user="frank"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, user, what, OTHER)
        return out.getvalue()

    def test_the_slot_is_free_beyond_those_waiting(self):
        self.assertEqual(dcc.nicks_waiting_for_a_slot("frank"), ["erin"])
        self.assertTrue(dcc.a_slot_is_free_beyond_those_waiting("frank"))

    def test_a_newcomers_large_file_is_queued_behind_her(self):
        log = self.ask(BIG_TWO)
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["hank"])
        self.assertNotIn("frank", config.user_processing_lock)
        self.assertEqual([r["file"] for r in config.dcc_queue["frank"]], [BIG_TWO])
        self.assertIn(self.WAITED_LINE, log)
        self.assertNotIn(LARGE_LINE, log)

    def test_a_small_one_still_goes_at_once(self):
        log = self.ask(SMALL)
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["hank", "frank"])
        self.assertNotIn("has waited longer with a large file", log)

    def test_off_the_large_one_goes_at_once(self):
        self.set_config(LARGE_FILE_MB=0)
        log = self.ask(BIG_TWO)
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["hank", "frank"])
        self.assertNotIn("has waited longer with a large file", log)

    def test_a_nick_waiting_with_a_small_file_next_takes_no_large_turn(self):
        """Only a waiting nick whose next file is large keeps the large turn."""
        config.dcc_queue["erin"] = [self.row("erin", SMALL, 4096)]
        self.assertTrue(dcc.a_slot_is_free_beyond_those_waiting("frank"))
        log = self.ask(BIG_TWO)
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["hank", "frank"])
        self.assertNotIn("frank", config.dcc_queue)
        self.assertNotIn("has waited longer with a large file", log)

    def test_a_held_back_nick_is_no_one_to_wait_for(self):
        """While a large file is sent erin is held back, and it is that
        send, not erin, that queues frank's file."""
        self.running(RUNNING_BIG)
        log = self.ask(BIG_TWO)
        self.assertEqual([r["file"] for r in config.dcc_queue["frank"]], [BIG_TWO])
        self.assertIn("frank's " + LARGE_LINE, log)
        self.assertNotIn("has waited longer with a large file", log)


class OnlyARequestSentNowIsJudged(Case):
    """The rule is asked of a request that would be sent now: one queued
    behind the nick's own files is not "held back", and the log does not
    say it is."""

    def test_a_large_file_behind_the_nicks_own_says_nothing_of_it(self):
        self.queue(dave=self.row("dave", SMALL, 4096))
        self.running(RUNNING_BIG)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, "dave", BIG, OTHER)
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]], [SMALL, BIG])
        self.assertNotIn(LARGE_LINE, out.getvalue())
        self.assertNotIn("has waited longer with a large file", out.getvalue())

    def test_nor_one_queued_with_every_slot_taken(self):
        self.running(RUNNING_BIG, RUNNING_SMALL, dict(RUNNING_SMALL, user="ivan"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, "dave", BIG, OTHER)
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]], [BIG])
        self.assertNotIn(LARGE_LINE, out.getvalue())


class TheRealSizeWinsOnceTheSendHasReadIt(Case):
    """A transfer's recorded size is a stand-in until start_dcc_send() reads
    the real one; where the two disagree, the real one counts."""

    def setUp(self):
        super().setUp()
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))

    def test_a_real_small_size_beats_a_large_recorded_one(self):
        self.running(dict(RUNNING_BIG, size=4096, **{dcc.QUEUED_SIZE_KEY: 5 * MB}))
        self.assertEqual(self.claim()[0], "dave")

    def test_also_when_the_recorded_one_is_on_its_row(self):
        row = self.row("gina", "Huge Archive.flac", 5 * MB)
        self.running({"user": "gina", "file": "Huge Archive.flac", "bytes_sent": 0, "size": 4096,
                      "queue_row": row})
        self.assertEqual(self.claim()[0], "dave")

    def test_a_real_large_size_beats_a_small_recorded_one(self):
        self.running(dict(RUNNING_BIG, size=5 * MB, **{dcc.QUEUED_SIZE_KEY: 4096}))
        self.assertEqual(self.claim()[0], "erin")


class TheFillPassOnARealThread(Case):
    """The same, with the pass on the thread the daemon starts it on: wait
    for the claim it makes, then join every thread the test started."""

    def test_the_second_nick_is_claimed(self):
        self.set_config(MAX_DCC_SLOTS=4)
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096),
                   frank=self.row("frank", SMALL_TWO, 4096))
        self.running(RUNNING_BIG)

        started = []
        frank_sent = threading.Event()
        threads = []
        real_thread = self._real_thread

        class Recorded(real_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                threads.append(self)

        def fake_send(sock, user, path, name, chan, row, *rest):
            started.append(user)
            if user == "frank":
                frank_sent.set()

        dcc.threading.Thread = Recorded
        self.addCleanup(setattr, dcc.threading, "Thread", feed.InlineThread)
        sends = mock.patch.object(dcc, "start_dcc_send", fake_send)
        sends.start()
        self.addCleanup(sends.stop)

        with contextlib.redirect_stdout(io.StringIO()):
            dcc.check_queue_and_send(self.sock, SWEEP)
            waited = frank_sent.wait(10.0)
            # Every thread, including any a joined one started before it ended.
            index = 0
            while index < len(threads):
                threads[index].join(10.0)
                index += 1
        dcc.threading.Thread = feed.InlineThread

        self.assertTrue(waited, "the fill pass never claimed frank")
        self.assertEqual([t for t in threads if t.is_alive()], [])
        self.assertEqual(sorted(started), ["erin", "frank"])
        self.assertEqual(sorted(tx["user"] for tx in config.active_transfers), ["erin", "frank", "gina"])
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]], [BIG])


class TheOperatorSeesWhy(Case):
    """The console's queue and the dashboard's Queue page name the wait."""

    def setUp(self):
        super().setUp()
        self.queue(dave=self.row("dave", BIG, MB), erin=self.row("erin", SMALL, 4096))

    def statuses(self):
        return {row["user"]: row["status"] for row in web_queue.build_queue_payload()}

    def console(self, args):
        session = mock.Mock()
        session.sent = []
        session.send = session.sent.append
        adminchat._cmd_queue(session, args)
        return session.sent

    def test_the_queue_page_says_large_wait(self):
        self.running(RUNNING_BIG)
        self.assertEqual(self.statuses(), {"dave": "large-wait", "erin": "queued", "gina": "sending"})
        self.assertEqual(web_queue.build_queue_payload(user="dave")["status"], "large-wait")
        self.assertEqual(web_queue.build_queue_payload(user="erin")["status"], "queued")

    def test_queued_when_the_rule_is_off(self):
        self.set_config(LARGE_FILE_MB=0)
        self.running(RUNNING_BIG)
        self.assertEqual(self.statuses()["dave"], "queued")
        self.assertEqual(web_queue.build_queue_payload(user="dave")["status"], "queued")

    def test_queued_when_no_large_file_is_being_sent(self):
        self.running(RUNNING_SMALL)
        self.assertEqual(self.statuses()["dave"], "queued")

    def test_frozen_and_sending_still_win(self):
        self.running(RUNNING_BIG)
        config.frozen_queues["dave"] = 1.0
        self.assertEqual(self.statuses()["dave"], "frozen")
        self.assertEqual(web_queue.build_queue_payload(user="dave")["status"], "frozen")
        del config.frozen_queues["dave"]
        self.running({"user": "dave", "file": SMALL, "bytes_sent": 0, "size": 4096})
        self.assertEqual(self.statuses()["dave"], "sending")
        self.assertEqual(web_queue.build_queue_payload(user="dave")["status"], "sending")

    def test_the_console_summary_notes_it(self):
        self.running(RUNNING_BIG)
        lines = {line.split()[0]: line for line in self.console("") if line.startswith("  ")}
        self.assertTrue(lines["dave"].endswith("  " + dcc.LARGE_FILE_WAIT_NOTE), lines["dave"])
        self.assertNotIn(dcc.LARGE_FILE_WAIT_NOTE, lines["erin"])

    def test_and_so_does_one_nicks_view(self):
        self.running(RUNNING_BIG)
        self.assertEqual(self.console("dave")[0], f"dave: 1 file(s) ({dcc.LARGE_FILE_WAIT_NOTE})")
        self.assertEqual(self.console("erin")[0], "erin: 1 file(s)")

    def test_the_console_says_nothing_while_no_large_file_is_sent(self):
        self.running(RUNNING_SMALL)
        self.assertNotIn(dcc.LARGE_FILE_WAIT_NOTE, "\n".join(self.console("")))
        self.assertEqual(self.console("dave")[0], "dave: 1 file(s)")


class WaitsForALargeSend(Case):
    """dcc.waits_for_a_large_send(), what the console and the page read."""

    def test_a_large_head_while_a_large_file_is_sent(self):
        rows = [self.row("dave", BIG, MB), self.row("dave", SMALL, 4096)]
        self.assertTrue(dcc.waits_for_a_large_send(rows, [RUNNING_BIG]))

    def test_only_the_head_counts(self):
        rows = [self.row("dave", SMALL, 4096), self.row("dave", BIG, MB)]
        self.assertFalse(dcc.waits_for_a_large_send(rows, [RUNNING_BIG]))

    def test_not_without_a_large_send(self):
        self.assertFalse(dcc.waits_for_a_large_send([self.row("dave", BIG, MB)], [RUNNING_SMALL]))
        self.assertFalse(dcc.waits_for_a_large_send([self.row("dave", BIG, MB)], []))

    def test_the_copy_handed_in_is_read_not_the_live_list(self):
        self.running(RUNNING_BIG)
        self.assertFalse(dcc.waits_for_a_large_send([self.row("dave", BIG, MB)], []))
        self.assertTrue(dcc.waits_for_a_large_send([self.row("dave", BIG, MB)]))

    def test_not_while_off_or_for_an_empty_queue(self):
        self.assertFalse(dcc.waits_for_a_large_send([], [RUNNING_BIG]))
        self.set_config(LARGE_FILE_MB=0)
        self.assertFalse(dcc.waits_for_a_large_send([self.row("dave", BIG, MB)], [RUNNING_BIG]))

    def test_a_row_that_is_not_a_dict_is_small(self):
        self.assertFalse(dcc.waits_for_a_large_send(["Big One.flac"], [RUNNING_BIG]))


if __name__ == "__main__":
    unittest.main()
