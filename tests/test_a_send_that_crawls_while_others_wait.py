"""#1388: a send that crawls while every slot is taken and others wait is stopped.

SLOW_SEND_MIN_KB, 0 (off) by default. Above 0, a send whose receiver has
acknowledged less than that over about the last minute is stopped - judged
from a minute after it connected, only while every slot is taken and a nick
is waiting for one, and never while the bot's own upload cap (#1352) is what
holds that send back (#1436: only while it moves about an even share of a
full cap). The row is kept and charged against MAX_SEND_FAILS like
a stall, the nick goes to the back, it is told why, and the queue is woken
three seconds later rather than after the failed-row back-off.

Slow has to be the receiver's fault: each send publishes its window rate on
its transfer row, and one is stopped only while ANOTHER send moved at least
the floor (the bot's side can deliver) and it is the slowest of the rated
sends, ties kept. A row stopped for its speed is marked, and a nick waiting
with a marked row at its head does not count as waiting, so two crawlers do
not trade a slot back and forth.

Four layers, all without a socket or a real clock, so every runner runs them:
the watch itself (dcc._SlowSendWatch) driven by a fake clock; the send loop
(stream_the_file) with a connection whose every chunk takes a second of that
clock; finish_send() with a real queue row; and start_dcc_send() whole, with
a fake listener whose receiver connects and crawls. Beside them, the real
upload cap's clock (speed_cap.take()) on a fake time, whose measured rates
are what upload_cap_holds_this_send() is asked about (#1436).

Every nick, channel and file name here is invented.
"""

import contextlib
import io
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import speed_cap  # noqa: E402

from tests import dcc_ports  # noqa: E402
from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_dcc_resume_end_to_end import RecordingIrcSocket  # noqa: E402

KB = 1024
SENDER = "kestrel"          # the send being judged
OTHER = "plover"            # the send in the other slot, delivering
WAITER = "wren"             # the nick waiting for a slot
CHANNEL = "#somechannel"
PORT_START, PORT_END = dcc_ports(51340, 51350)
RATE = dcc.ACKED_RATE
MARK = dcc.STOPPED_FOR_SPEED


class FakeClock:
    """What the watch reads for "now": moves only when a test moves it."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class WithEverySlotTaken(DCCoreTestCase):
    """Two slots, both sending - OTHER's already rated at 100 KB/s, proof the
    bot's side delivers - and WAITER in the channel with a file queued: the
    one case the watch is allowed to stop a send in."""

    def setUp(self):
        super().setUp()
        self.set_config(SLOW_SEND_MIN_KB=10, MAX_DCC_SLOTS=2, MAX_UPLOAD_KB=0,
                        MAX_SEND_FAILS=3)
        self.mine = {"user": SENDER, "file": "Some_Album.flac", "bytes_sent": 0}
        self.other = {"user": OTHER, "file": "other.flac", "bytes_sent": 0, RATE: 100.0 * KB}
        config.active_transfers[:] = [self.mine, self.other]
        config.dcc_queue[WAITER] = [{"file": "Another_Album.flac", "user": WAITER}]
        config.channel_users[CHANNEL] = {SENDER, OTHER, WAITER}
        self.oserve.total_sent_bytes = 0


class Send:
    """One running send for the multi-send drives: its transfer row, its
    watch, and how fast its receiver acknowledges."""

    def __init__(self, user, kb_per_second, clock, acked=0):
        self.row = {"user": user, "file": user + ".flac", "bytes_sent": 0}
        self.kb_per_second = kb_per_second
        self.acked = acked
        self.watch = dcc._SlowSendWatch(acked, clock=clock, row=self.row)
        self.stopped_at = None


class TheWatch(WithEverySlotTaken):
    """dcc._SlowSendWatch on its own, one check a second."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.acked = 0
        self.watch = dcc._SlowSendWatch(self.acked, clock=self.clock, row=self.mine)

    def drive(self, seconds, kb_per_second, uploaded_per_second=0, watch=None):
        """Move the clock a second at a time for `seconds`, the receiver
        acknowledging `kb_per_second` and every send together putting
        `uploaded_per_second` bytes on the wire. Returns (seconds since
        connect, speed) at the first check that stops the send, else None."""
        watch = watch or self.watch
        for _ in range(int(seconds)):
            self.clock.t += 1.0
            self.acked += int(kb_per_second * KB)
            self.oserve.total_sent_bytes += int(uploaded_per_second)
            speed = watch.check(self.acked)
            if speed is not None:
                return self.clock.t - watch.connected_at, speed
        return None

    def sends(self, *kb_per_seconds):
        """Fresh sends, one per rate, filling every slot between them."""
        made = [Send(f"sender{n}", kbps, self.clock) for n, kbps in enumerate(kb_per_seconds)]
        config.active_transfers[:] = [send.row for send in made]
        self.set_config(MAX_DCC_SLOTS=len(made))
        return made

    def drive_all(self, sends, seconds):
        """Every send in turn, a second at a time; a stopped one stays in
        active_transfers (its finish_send() has not run) and is not driven."""
        for _ in range(int(seconds)):
            self.clock.t += 1.0
            for send in sends:
                if send.stopped_at is not None:
                    continue
                send.acked += int(send.kb_per_second * KB)
                if send.watch.check(send.acked) is not None:
                    send.stopped_at = self.clock.t - send.watch.connected_at
        return [send for send in sends if send.stopped_at is not None]

    # -- when it may judge at all -------------------------------------------

    def test_full_slots_and_a_waiting_nick_stop_a_crawl_at_one_minute(self):
        stopped = self.drive(120, kb_per_second=1)
        self.assertIsNotNone(stopped, "a 1 KB/s send under a 10 KB/s floor ran on")
        at, speed = stopped
        self.assertEqual(at, 60.0, "judged from the first check a minute after it connected")
        self.assertEqual(speed, 1.0 * KB)

    def test_nothing_is_judged_inside_the_first_minute(self):
        self.assertIsNone(self.drive(59, kb_per_second=0))
        self.assertIsNotNone(self.drive(1, kb_per_second=0), "and at 60.0 s it is")

    def test_off_never_stops_anything(self):
        self.set_config(SLOW_SEND_MIN_KB=0)
        self.assertIsNone(self.drive(300, kb_per_second=0))

    def test_a_free_slot_lets_it_take_as_long_as_it_takes(self):
        self.set_config(MAX_DCC_SLOTS=3)
        self.assertIsNone(self.drive(300, kb_per_second=0))

    def test_full_slots_with_nobody_queued_keep_it(self):
        config.dcc_queue.clear()
        self.assertIsNone(self.drive(300, kb_per_second=0))

    def test_a_waiting_nick_that_left_the_channel_keeps_it(self):
        config.channel_users[CHANNEL] = {SENDER, OTHER}
        self.assertIsNone(self.drive(300, kb_per_second=0))

    def test_a_frozen_waiting_queue_keeps_it(self):
        config.frozen_queues[WAITER] = 1.0
        self.assertIsNone(self.drive(300, kb_per_second=0))

    def test_a_pack_waiting_while_one_is_being_made_keeps_it(self):
        """A folder-pack head can start only when no pack is being made, as
        check_queue_and_send() has it: nobody the freed slot would go to."""
        config.dcc_queue[WAITER] = [{"file": "Some_Folder", "user": WAITER, "is_unpacked_rar_folder": True}]
        config.rar_inprogress = True
        self.assertIsNone(self.drive(300, kb_per_second=0))

    def test_a_pack_waiting_with_the_packer_free_stops_it(self):
        config.dcc_queue[WAITER] = [{"file": "Some_Folder", "user": WAITER, "is_unpacked_rar_folder": True}]
        config.rar_inprogress = False
        self.assertIsNotNone(self.drive(120, kb_per_second=0))

    def test_it_looks_no_more_often_than_every_half_second(self):
        """check() runs per chunk; a sample, and the lock others_wait_for_a_slot()
        takes, at most once per SLOW_SEND_CHECK_SECONDS."""
        asked = []
        start = self.clock.t
        with mock.patch.object(dcc, "others_wait_for_a_slot", lambda: asked.append(1) and False):
            for tick in range(1, 701):           # 70 s in 0.1 s steps: 60 grace, 10 judged
                self.clock.t = start + tick / 10
                self.acked += 100
                self.assertIsNone(self.watch.check(self.acked))
        self.assertGreaterEqual(len(asked), 18)
        self.assertLessEqual(len(asked), 21, "asked more than once per half second")
        self.assertLessEqual(len(self.watch.samples), 2 + int(dcc.SLOW_SEND_WINDOW_SECONDS / 0.5),
                             "a sample per chunk, not per half second")

    # -- the speed --------------------------------------------------------------

    def test_exactly_the_floor_is_fast_enough(self):
        self.assertIsNone(self.drive(300, kb_per_second=10))

    def test_just_under_the_floor_is_not(self):
        self.assertIsNotNone(self.drive(120, kb_per_second=9))

    def test_a_slow_start_that_recovers_inside_the_window_is_kept(self):
        """30 s of nothing, then fast: the minute's rate is what counts."""
        self.assertIsNone(self.drive(30, kb_per_second=0))
        self.assertIsNone(self.drive(270, kb_per_second=40))

    def test_a_fast_half_minute_then_a_crawl_is_judged_over_the_whole_minute(self):
        """30 s at 30 KB/s, then 1 KB/s: at the 60 s mark the minute averages
        15.5 KB/s, over the floor. Only a half-minute window says 1 KB/s."""
        self.assertIsNone(self.drive(30, kb_per_second=30))
        self.assertIsNone(self.drive(30, kb_per_second=1))

    def test_the_window_slides_so_an_old_crawl_is_forgiven(self):
        """A minute of nothing with nobody waiting, then 15 KB/s: the last
        minute is over the floor, the whole send (7.5 KB/s) is not."""
        config.dcc_queue.clear()
        self.assertIsNone(self.drive(60, kb_per_second=0))
        self.assertIsNone(self.drive(60, kb_per_second=15))
        config.dcc_queue[WAITER] = [{"file": "Another_Album.flac", "user": WAITER}]
        self.assertIsNone(self.drive(60, kb_per_second=15))

    def test_the_window_slides_so_a_new_crawl_is_caught(self):
        """A fast minute with nobody waiting, then 1 KB/s: the whole send
        averages far over the floor, the last minute is under it."""
        config.dcc_queue.clear()
        self.assertIsNone(self.drive(60, kb_per_second=100))
        config.dcc_queue[WAITER] = [{"file": "Another_Album.flac", "user": WAITER}]
        stopped = self.drive(120, kb_per_second=1)
        self.assertIsNotNone(stopped, "a send that fell to 1 KB/s ran on on its old average")
        at, speed = stopped
        self.assertLess(speed, 10 * KB)

    def test_a_resume_is_measured_from_where_it_resumed(self):
        """A send resumed at 10 MB that crawls is stopped at 60.0 s at its own
        rate: the 10 MB the receiver already held is not a burst."""
        self.acked = 10 * KB * KB
        self.watch = dcc._SlowSendWatch(self.acked, clock=self.clock, row=self.mine)
        stopped = self.drive(120, kb_per_second=1)
        self.assertIsNotNone(stopped, "the resume offset was counted as speed")
        self.assertEqual(stopped, (60.0, 1.0 * KB))

    # -- its rate, published --------------------------------------------------

    def test_its_rate_is_published_only_after_the_grace_and_with_the_setting_off(self):
        self.set_config(SLOW_SEND_MIN_KB=0)
        self.drive(59, kb_per_second=3)
        self.assertNotIn(RATE, self.mine, "rated inside the first minute")
        self.drive(1, kb_per_second=3)
        self.assertEqual(self.mine.get(RATE), 3.0 * KB, "turning it on must find every send rated")

    # -- slow has to be the receiver's fault -----------------------------------

    def test_a_full_uplink_with_every_send_under_the_floor_stops_nobody(self):
        """The NAS or the uplink probe: two sends at 40 KB/s under a 50 KB/s
        floor. Nobody delivers the floor, so it is the bot's side."""
        self.set_config(SLOW_SEND_MIN_KB=50)
        sends = self.sends(40, 40)
        self.assertEqual(self.drive_all(sends, 300), [])
        self.assertEqual([send.row.get(RATE) for send in sends], [40.0 * KB] * 2, "both were rated")

    def test_a_cap_above_what_the_line_really_does_stops_nobody(self):
        self.set_config(SLOW_SEND_MIN_KB=50, MAX_UPLOAD_KB=1000)
        sends = self.sends(40, 40)
        self.assertEqual(self.drive_all(sends, 300), [])

    def test_with_no_other_send_at_the_floor_nothing_is_stopped(self):
        self.other[RATE] = 9.0 * KB
        self.assertIsNone(self.drive(300, kb_per_second=1))

    def test_another_send_exactly_at_the_floor_is_proof_enough(self):
        self.other[RATE] = 10.0 * KB
        self.assertIsNotNone(self.drive(120, kb_per_second=1))

    def test_an_unrated_other_send_is_no_proof(self):
        del self.other[RATE]
        self.assertIsNone(self.drive(300, kb_per_second=1))

    def test_only_the_slowest_is_stopped_and_only_one_per_decision(self):
        """80, 4 and 6 KB/s under a 10 KB/s floor: the 4 goes. While its row is
        still running (finish_send() not yet through), the 6 is not the
        slowest and stays."""
        fast, slowest, slow = self.sends(80, 4, 6)
        self.assertEqual(self.drive_all([fast, slowest, slow], 300), [slowest])
        self.assertEqual(slowest.stopped_at, 60.0, "judged at its first check, the 80 rated just before")
        self.assertIsNone(slow.stopped_at)

    def test_two_rated_crawlers_at_the_same_rate_are_both_kept(self):
        fast, one, two = self.sends(80, 4, 4)
        one.row[RATE] = two.row[RATE] = 4.0 * KB      # both rated already
        self.assertEqual(self.drive_all([fast, one, two], 300), [])

    def test_one_slot_never_fires(self):
        self.set_config(MAX_DCC_SLOTS=1)
        config.active_transfers[:] = [self.mine]
        self.assertIsNone(self.drive(300, kb_per_second=0))

    def test_one_rated_send_among_unrated_ones_never_fires(self):
        self.set_config(MAX_DCC_SLOTS=3)
        config.active_transfers[:] = [self.mine, {"user": OTHER, "file": "a.flac"},
                                      {"user": "teal", "file": "b.flac"}]
        self.assertIsNone(self.drive(300, kb_per_second=0))

    def test_a_watch_without_a_row_never_fires(self):
        watch = dcc._SlowSendWatch(self.acked, clock=self.clock)
        self.assertIsNone(self.drive(300, kb_per_second=0, watch=watch))

    # -- not twice for the same trade -------------------------------------------

    def test_a_waiter_whose_next_row_was_stopped_for_speed_does_not_count(self):
        config.dcc_queue[WAITER][0][MARK] = True
        self.assertIsNone(self.drive(300, kb_per_second=0))

    # -- the bot's own cap ------------------------------------------------------
    #
    # A full cap shields only the sends it holds (#1436): a send is kept while
    # it moves at least SLOW_SEND_CAP_NEAR of an even split of what every send
    # together moved, over the same window; under that it is judged as with no
    # cap at all.

    def sends_bps(self, *bytes_per_second):
        """sends(), with each rate in bytes per second, not KB/s."""
        return self.sends(*(bps / KB for bps in bytes_per_second))

    def drive_on_the_wire(self, sends, seconds):
        """drive_all(), with every byte a send's receiver acknowledges also
        put on the wire (oserve.total_sent_bytes), as a real send does - so
        what every send together moved is the sum of the running sends."""
        for _ in range(int(seconds)):
            self.clock.t += 1.0
            for send in sends:
                if send.stopped_at is not None:
                    continue
                step = round(send.kb_per_second * KB)
                send.acked += step
                self.oserve.total_sent_bytes += step
                if send.watch.check(send.acked) is not None:
                    send.stopped_at = self.clock.t - send.watch.connected_at
        return [send for send in sends if send.stopped_at is not None]

    def test_the_audit_case_a_crawler_beside_two_sends_filling_the_cap_is_stopped(self):
        """MAX_UPLOAD_KB=300, SLOW_SEND_MIN_KB=60, three slots: 140, 140 and
        20 KB/s, the cap full at 300. The 20 is held by its own link, not the
        cap (its even share is 100), and is stopped after the minute's grace
        - just as it is with the cap off. Before #1436 it kept its slot for as
        long as the two fast sends kept the cap full."""
        for cap in (300, 0):
            with self.subTest(MAX_UPLOAD_KB=cap):
                self.set_config(MAX_UPLOAD_KB=cap, SLOW_SEND_MIN_KB=60)
                fast, faster, crawler = self.sends(140, 140, 20)
                self.assertEqual(self.drive_on_the_wire([fast, faster, crawler], 180), [crawler])
                self.assertEqual(crawler.stopped_at, 60.0)
                self.assertEqual(crawler.row[RATE], 20.0 * KB)

    def test_a_crawler_beside_one_send_filling_the_cap_is_stopped(self):
        """The case this module's cap test used to assert was KEPT: cap 100,
        two slots, this send at 1 KB/s beside OTHER, every send together at
        95 KB/s. That was the #1436 defect - OTHER filled the cap; this one
        was held by its own link."""
        self.set_config(MAX_UPLOAD_KB=100)
        self.assertEqual(self.drive(120, kb_per_second=1, uploaded_per_second=0.95 * 100 * KB),
                         (60.0, 1.0 * KB))

    def test_equal_shares_of_a_full_cap_are_kept(self):
        """The case #1388 meant to keep, at a full cap (150) and at exactly
        SLOW_SEND_CAP_NEAR of it (135). Three sends the cap holds about evenly,
        the slowest under the floor and another at it: the cap, not the
        slowest receiver, is what makes it slow - and with the cap off the
        same slowest send is stopped.

        This test used to be "cap 100, this send 1 KB/s beside one at 100,
        every send 95 KB/s: kept" - the #1436 defect itself, a crawler on its
        own link shielded because another send filled the cap. That case is
        now asserted stopped, above."""
        for cap, kb_per_seconds, floor in ((150, (48, 50, 52), 51),
                                           (150, (42, 45, 48), 46)):
            with self.subTest(total=sum(kb_per_seconds), of_cap=cap):
                self.set_config(MAX_UPLOAD_KB=cap, SLOW_SEND_MIN_KB=floor)
                sends = self.sends(*kb_per_seconds)
                self.assertEqual(self.drive_on_the_wire(sends, 300), [])
                self.assertEqual([send.row[RATE] for send in sends],
                                 [kbps * KB for kbps in kb_per_seconds], "every send was rated")
                self.set_config(MAX_UPLOAD_KB=0)
                sends = self.sends(*kb_per_seconds)
                self.assertEqual(self.drive_on_the_wire(sends, 300), [sends[0]],
                                 "without the cap the same slowest send is stopped")

    def test_three_even_shares_under_the_floor_are_kept(self):
        """Cap 150 full, floor 60, three at 50; and at exactly 90% of the cap,
        three at 45. Kept by the cap and by nobody reaching the floor."""
        for each in (50, 45):
            with self.subTest(each=each):
                self.set_config(MAX_UPLOAD_KB=150, SLOW_SEND_MIN_KB=60)
                self.assertEqual(self.drive_on_the_wire(self.sends(each, each, each), 300), [])

    def test_exactly_the_split_of_a_full_cap_is_kept_and_a_byte_under_it_is_not(self):
        """Cap 300 KB/s, full; floor 100 KB/s; three sends. The even split's
        share is SLOW_SEND_CAP_NEAR x 300 / 3 = 90 KB/s: a send at exactly
        that is kept, one a byte per second under it - the slowest, the other
        two over the floor - is stopped."""
        self.set_config(MAX_UPLOAD_KB=300, SLOW_SEND_MIN_KB=100)
        total = 300 * KB
        split = dcc.SLOW_SEND_CAP_NEAR * total / 3
        self.assertEqual(split, 90 * KB, "the boundary is exact in floating point")
        for speed, stopped in ((split, False), (split - 1, True)):
            with self.subTest(speed=speed):
                rest = int(total - speed)
                sends = self.sends_bps(rest // 2, rest - rest // 2, speed)
                self.assertGreaterEqual(min(sends[0].kb_per_second, sends[1].kb_per_second), 100)
                self.assertEqual(self.drive_on_the_wire(sends, 180), [sends[2]] if stopped else [])
                self.assertEqual(sends[2].row[RATE], speed)
                if stopped:
                    self.assertEqual(sends[2].stopped_at, 60.0, "at its first check, the others rated just before")

    def test_a_row_that_moves_nothing_yet_lowers_the_split(self):
        """Four rows, one still connecting (nothing acknowledged, no rate):
        cap 300 full, 80 / 110 / 110 KB/s, floor 100. The split is over every
        running row, 0.9 x 300 / 4 = 67.5 KB/s, so the 80 is kept; split over
        the three that move (90 KB/s) it would be stopped."""
        self.set_config(MAX_UPLOAD_KB=300, SLOW_SEND_MIN_KB=100)
        sends = self.sends(80, 110, 110)
        connecting = {"user": "teal", "file": "teal.flac", "bytes_sent": 0}
        config.active_transfers.append(connecting)
        self.set_config(MAX_DCC_SLOTS=4)
        self.assertEqual(self.drive_on_the_wire(sends, 300), [])
        self.assertNotIn(RATE, connecting)
        sends = self.sends(80, 110, 110)
        self.assertEqual(self.drive_on_the_wire(sends, 300), [sends[0]],
                         "and with the row gone the 80 is under its share")

    def test_a_cap_the_sends_are_nowhere_near_does_not_shield_it(self):
        self.set_config(MAX_UPLOAD_KB=100)
        self.assertIsNotNone(self.drive(120, kb_per_second=1, uploaded_per_second=0.2 * 100 * KB))

    def test_a_cap_that_is_not_full_does_not_shield_even_a_near_even_share(self):
        """45 and 50 KB/s under a 50 KB/s floor, 95 KB/s together: 45 is over
        an even split's share (42.75) but the cap (1000, or none) is not full,
        so it holds nobody back - the 45 is stopped."""
        for cap in (1000, 0):
            with self.subTest(MAX_UPLOAD_KB=cap):
                self.set_config(MAX_UPLOAD_KB=cap, SLOW_SEND_MIN_KB=50)
                sends = self.sends(50, 45)
                self.assertEqual(self.drive_on_the_wire(sends, 180), [sends[1]])
                self.assertEqual(sends[1].stopped_at, 60.0)


class TheHelpers(WithEverySlotTaken):

    def test_the_floor_is_the_setting_in_bytes_and_anything_odd_is_off(self):
        for value, floor in ((10, 10 * KB), (2.5, 2.5 * KB), (0, 0.0), (-5, 0.0),
                             (None, 0.0), ("soon", 0.0), ("12", 12 * KB)):
            with self.subTest(value=value):
                self.set_config(SLOW_SEND_MIN_KB=value)
                self.assertEqual(dcc.slow_send_floor(), floor)

    def test_the_cap_binds_from_ninety_percent_of_it_and_only_when_set(self):
        self.set_config(MAX_UPLOAD_KB=100)
        cap = 100 * KB
        self.assertTrue(dcc.upload_cap_binds(cap * dcc.SLOW_SEND_CAP_NEAR))
        self.assertTrue(dcc.upload_cap_binds(cap))
        self.assertFalse(dcc.upload_cap_binds(cap * dcc.SLOW_SEND_CAP_NEAR - 1))
        self.set_config(MAX_UPLOAD_KB=0)
        self.assertFalse(dcc.upload_cap_binds(10 ** 9), "no cap, nothing it could bind")

    def test_the_cap_holds_a_send_only_at_its_share_of_a_full_cap(self):
        """upload_cap_holds_this_send(speed, total_bps) (#1436): the cap binds
        AND speed >= SLOW_SEND_CAP_NEAR x total / every running row."""
        self.set_config(MAX_UPLOAD_KB=150)
        config.active_transfers[:] = [self.mine, self.other, {"user": "teal", "file": "c.flac"}]
        holds = dcc.upload_cap_holds_this_send
        edge = dcc.SLOW_SEND_CAP_NEAR * 150 * KB / 3
        # Full: 150 KB/s by three, an even 50 each; a share of 45 is the edge.
        self.assertTrue(holds(50 * KB, 150 * KB))
        self.assertTrue(holds(edge, 150 * KB), "exactly the edge")
        self.assertFalse(holds(edge - 1, 150 * KB))
        self.assertFalse(holds(20 * KB, 150 * KB), "a crawler while the others fill the cap")
        # At exactly SLOW_SEND_CAP_NEAR of the cap (135): split the measured
        # total, 40.5 each, not the cap's 45.
        self.assertTrue(holds(45 * KB, 135 * KB))
        self.assertTrue(holds(42 * KB, 135 * KB), "over 0.9 x 135 / 3, under 0.9 x 150 / 3")
        self.assertFalse(holds(40 * KB, 135 * KB))
        # Under 90% of the cap it holds nobody, whatever the share.
        self.assertFalse(holds(130 * KB, 135 * KB - 1))
        self.assertFalse(holds(100 * KB, 100 * KB), "far from full, even the whole total")
        # A fourth row, moving nothing yet, lowers the split to 0.9 x 150 / 4.
        config.active_transfers.append({"user": "smew", "file": "d.flac"})
        self.assertTrue(holds(34 * KB, 150 * KB))
        self.assertFalse(holds(33 * KB, 150 * KB))
        # One row (or, read mid-change, none): the split is the whole total.
        config.active_transfers[:] = [self.mine]
        whole = dcc.SLOW_SEND_CAP_NEAR * 150 * KB
        self.assertTrue(holds(whole, 150 * KB))
        self.assertFalse(holds(whole - 1, 150 * KB))
        config.active_transfers[:] = []
        self.assertTrue(holds(whole, 150 * KB), "no division by zero")
        # No cap: nothing it could hold.
        self.set_config(MAX_UPLOAD_KB=0)
        config.active_transfers[:] = [self.mine, self.other]
        self.assertFalse(holds(10 ** 9, 10 ** 9))
        self.assertFalse(holds(0.0, 0.0))

    def test_others_wait_only_with_every_slot_taken_and_a_nick_present(self):
        self.assertTrue(dcc.others_wait_for_a_slot())
        self.set_config(MAX_DCC_SLOTS=3)
        self.assertFalse(dcc.others_wait_for_a_slot(), "a slot is free")
        self.set_config(MAX_DCC_SLOTS=1)
        self.assertTrue(dcc.others_wait_for_a_slot(), "more sends than slots is still full")
        config.channel_users[CHANNEL] = {SENDER, OTHER}
        self.assertFalse(dcc.others_wait_for_a_slot(), "the waiting nick left")

    def test_a_marked_waiter_does_not_count_and_an_unmarked_one_beside_it_does(self):
        config.dcc_queue[WAITER][0][MARK] = True
        self.assertFalse(dcc.others_wait_for_a_slot(), "the only waiter was itself stopped for speed")
        config.dcc_queue["teal"] = [{"file": "Third_Album.flac", "user": "teal"}]
        config.channel_users[CHANNEL].add("teal")
        self.assertTrue(dcc.others_wait_for_a_slot(), "an unmarked waiter still counts")

    def test_the_slowest_while_another_delivers(self):
        floor = 10 * KB
        self.assertTrue(dcc.the_slowest_while_another_delivers(self.mine, 1.0 * KB, floor))
        self.assertFalse(dcc.the_slowest_while_another_delivers(None, 1.0 * KB, floor))
        self.assertFalse(dcc.the_slowest_while_another_delivers(self.mine, 100.0 * KB, floor), "a tie")
        self.other[RATE] = floor - 1
        self.assertFalse(dcc.the_slowest_while_another_delivers(self.mine, 1.0 * KB, floor))
        self.mine[RATE] = 0.0
        self.other[RATE] = 100.0 * KB
        self.assertTrue(dcc.the_slowest_while_another_delivers(self.mine, 1.0 * KB, floor),
                        "its own published rate is not one of the others")

    def test_the_reason_names_the_setting_for_the_operator_and_not_for_the_user(self):
        self.set_config(SLOW_SEND_MIN_KB=5)
        operator = dcc.too_slow_reason(2048.0)
        user = dcc.too_slow_reason(2048.0, to_the_user=True)
        self.assertEqual(operator, "sent at 2.0 KB/s, under SLOW_SEND_MIN_KB (5 KB/s) "
                                   "while others waited for a slot")
        self.assertIn("2.0 KB/s", user)
        self.assertIn("minimum of 5 KB/s", user)
        self.assertNotIn("SLOW_SEND_MIN_KB", user)

    def test_the_user_is_told_it_stopped_and_stays_queued(self):
        self.set_config(SLOW_SEND_MIN_KB=5)
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.tell_them_it_was_too_slow(SENDER, "Some_Album.flac", 2048.0)
        self.assertEqual(len(self.oserve.queued), 1)
        nick, line, _vip = self.oserve.queued[0]
        self.assertEqual(nick, SENDER)
        self.assertTrue(line.startswith(f"NOTICE {SENDER} :"))
        self.assertIn("Stopped", line)
        self.assertIn("Some_Album.flac was arriving at 2.0 KB/s", line)
        self.assertIn("stays in your queue", line)
        self.assertTrue(line.endswith("\r\n"))

    def test_without_the_daemon_loaded_nobody_is_told_and_nothing_raises(self):
        with mock.patch.dict(sys.modules):
            sys.modules.pop("oserve", None)
            dcc.tell_them_it_was_too_slow(SENDER, "Some_Album.flac", 2048.0)
        self.assertEqual(self.oserve.queued, [])


class ChunkSender:
    """One send in TheSharedClock's drive: its thread calls speed_cap.take()
    for a chunk, sleeps the wait it is given - its own sleep, which holds
    only it - and then spends `link_bps` time getting the chunk to its
    receiver before it asks for the next one."""

    def __init__(self, name, link_bps, start, blocks=None):
        self.name = name
        self.link_bps = link_bps
        self.ready_at = start       # when it next calls take()
        self.slept = 0.0
        self.delivered = []         # (acknowledged at, bytes)
        # The block each pass asks chunk_size() for (#1461), the last one
        # kept for every pass after it; None reads dcc_block_size() every
        # pass, as stream_the_file() does.
        self.blocks = list(blocks) if blocks is not None else None

    def next_block(self):
        if self.blocks is None:
            return dcc.dcc_block_size()
        return self.blocks.pop(0) if len(self.blocks) > 1 else self.blocks[0]

    def sleep(self, seconds):
        self.slept += seconds

    def rate(self, since, until):
        """What its receiver acknowledged in [since, until), in bytes/s."""
        moved = sum(n for at, n in self.delivered if since <= at < until)
        return moved / (until - since)


class TheSharedClock(DCCoreTestCase):
    """upload_cap_holds_this_send() rests on how speed_cap.take() shares the
    line (#1436): every send reserves an equal chunk (chunk_size(), from one
    DCC_BLOCK_SIZE) from one clock, first come first served, so a send that
    only the cap slows gets a chunk per round like every other and moves at
    least an even split of what every send together moves. The other tests
    here feed in made-up rates; this one runs the real clock - on a fake
    time, one thread stepping every send in the order their calls would
    reach take() - measures the rates it gives, and puts them to the real
    decision."""

    START = 1000.0
    WARM_UP = 10.0
    FAST_LINK = 10 * KB * KB    # 10 MB/s: far above any cap here
    SLOW_LINK = 20 * KB         # 20 KB/s: the receiver is the bottleneck

    def setUp(self):
        super().setUp()
        self.set_config(DCC_BLOCK_SIZE=64 * KB, MAX_DCC_SLOTS=4, MAX_UPLOAD_KB=0)

    def drive(self, sends, seconds):
        """Every send, in the order its calls would reach take(), for
        `seconds` of the fake clock; each one's next chunk is what the send
        loop asks for, chunk_size(UPLOAD, block) - the block being
        dcc_block_size() unless the send was given its own."""
        runtime.speed_cap_clocks.clear()
        clock = FakeClock(self.START)
        end = self.START + seconds
        while True:
            send = min(sends, key=lambda s: s.ready_at)
            if send.ready_at >= end:
                return
            clock.t = send.ready_at
            nbytes = speed_cap.chunk_size(speed_cap.UPLOAD, send.next_block())
            slept_before = send.slept
            speed_cap.take(speed_cap.UPLOAD, nbytes, clock=clock, sleep=send.sleep)
            waited = send.slept - slept_before
            acked_at = send.ready_at + waited + nbytes / send.link_bps
            send.delivered.append((acked_at, nbytes))
            send.ready_at = acked_at

    def test_a_send_only_the_cap_slows_moves_an_even_share_and_is_held(self):
        """Three sends on fast links and one on a 20 KB/s link, every slot
        taken, the cap full: over the watch's window each of the three moves
        at least SLOW_SEND_CAP_NEAR of total / 4 - four, because
        upload_cap_holds_this_send() splits by every running row - and is
        held; the slow-link send moves far less and is not. 1000 KB/s also
        covers a chunk clamped to the 64 KB block."""
        for cap_kb in (150, 400, 1000):
            with self.subTest(MAX_UPLOAD_KB=cap_kb):
                self.set_config(MAX_UPLOAD_KB=cap_kb)
                capped = [ChunkSender(f"capped{n}", self.FAST_LINK, self.START)
                          for n in range(3)]
                slow = ChunkSender("slowlink", self.SLOW_LINK, self.START)
                sends = capped + [slow]
                window = dcc.SLOW_SEND_WINDOW_SECONDS
                self.drive(sends, self.WARM_UP + window)
                since = self.START + self.WARM_UP
                rates = {s.name: s.rate(since, since + window) for s in sends}
                total = sum(rates.values())
                cap = cap_kb * KB

                # The clock is shared: together they fill the cap, no more.
                self.assertTrue(dcc.upload_cap_binds(total), rates)
                self.assertLessEqual(total, cap * 1.02, rates)

                even = dcc.SLOW_SEND_CAP_NEAR * total / len(sends)
                for send in capped:
                    self.assertGreaterEqual(rates[send.name], even,
                                            f"{send.name} is short of an even share: {rates}")
                self.assertLess(rates[slow.name], even, rates)

                # And the decision these rates feed: a row per running send.
                config.active_transfers[:] = [{"user": s.name, "file": s.name + ".flac"}
                                              for s in sends]
                for send in capped:
                    self.assertTrue(dcc.upload_cap_holds_this_send(rates[send.name], total),
                                    f"{send.name} is held by the cap but not shielded: {rates}")
                self.assertFalse(dcc.upload_cap_holds_this_send(rates[slow.name], total),
                                 f"the slow link is shielded by a cap it never reaches: {rates}")


    # -- sends asking for different blocks (#1461) ---------------------------
    #
    # The even split rests on every send asking chunk_size() with the same
    # block. A send started before a rehash lowered DCC_BLOCK_SIZE, if it kept
    # the block it started with, took a bigger chunk per round of the shared
    # clock than the sends started after the change; they moved less than an
    # even split although only the cap held them. The issue's own table:
    # (cap KB/s, the older send's block, the newer sends' block).
    MIXED = ((400, 64 * KB, 8 * KB), (400, 64 * KB, 16 * KB), (150, 64 * KB, 4 * KB))

    def rates_of(self, sends):
        window = dcc.SLOW_SEND_WINDOW_SECONDS
        self.drive(sends, self.WARM_UP + window)
        since = self.START + self.WARM_UP
        return {s.name: s.rate(since, since + window) for s in sends}

    def held(self, sends, rates):
        """upload_cap_holds_this_send() for each send, a row per send."""
        config.active_transfers[:] = [{"user": s.name, "file": s.name + ".flac"} for s in sends]
        total = sum(rates.values())
        return {s.name: dcc.upload_cap_holds_this_send(rates[s.name], total) for s in sends}

    def test_a_send_that_kept_a_bigger_block_starves_the_ones_after_it(self):
        """The failure #1461 reports, on the real clock: one send at the old
        64 KB block and two at the new smaller one, all on fast links under a
        full cap. Each round of the clock moves one chunk of each, so the
        rates split as the chunks do - 285 / 57 / 57 KB/s at 400 KB/s with
        8 KB - and the newer two are short of SLOW_SEND_CAP_NEAR of an even
        split and NOT held by upload_cap_holds_this_send(). With
        SLOW_SEND_MIN_KB=60, every slot full and a nick waiting, the 57 KB/s
        send would be stopped as a crawler while the 285 proves the bot
        delivers - although its own link is fast and only the cap slowed it.

        This drives the harness only, so it cannot fail on the old dcc.py;
        it is the case stream_the_file() now prevents by asking every pass
        for the current block (test_the_blocks_the_send_loop_asks_for_after_
        a_change_share_the_cap_evenly, below, and
        test_the_send_loop_on_its_own.py's TheBlockIsReadEveryPass)."""
        for cap_kb, old_block, new_block in self.MIXED:
            with self.subTest(MAX_UPLOAD_KB=cap_kb, old=old_block, new=new_block):
                self.set_config(MAX_UPLOAD_KB=cap_kb)
                before = ChunkSender("before", self.FAST_LINK, self.START, blocks=[old_block])
                after = [ChunkSender(f"after{n}", self.FAST_LINK, self.START, blocks=[new_block])
                         for n in range(2)]
                sends = [before] + after
                rates = self.rates_of(sends)
                total = sum(rates.values())
                self.assertTrue(dcc.upload_cap_binds(total), rates)

                # The rates split as the chunks do.
                big = speed_cap.chunk_size(speed_cap.UPLOAD, old_block)
                small = speed_cap.chunk_size(speed_cap.UPLOAD, new_block)
                self.assertGreater(big, small)
                expected = cap_kb * KB * small / (big + 2 * small)
                for send in after:
                    self.assertAlmostEqual(rates[send.name], expected, delta=0.02 * cap_kb * KB,
                                           msg=rates)

                even = dcc.SLOW_SEND_CAP_NEAR * total / len(sends)
                held = self.held(sends, rates)
                self.assertTrue(held["before"], rates)
                for send in after:
                    self.assertLess(rates[send.name], even, rates)
                    self.assertFalse(held[send.name], f"{send.name} at {rates[send.name]:.0f} B/s: {rates}")

        # The issue's 57 KB/s, under a 60 KB/s floor that the older send clears.
        self.set_config(MAX_UPLOAD_KB=400)
        sends = [ChunkSender("before", self.FAST_LINK, self.START, blocks=[64 * KB])] + \
                [ChunkSender(f"after{n}", self.FAST_LINK, self.START, blocks=[8 * KB]) for n in range(2)]
        rates = self.rates_of(sends)
        self.assertAlmostEqual(rates["after0"] / KB, 57, delta=2)
        self.assertGreater(rates["before"], 60 * KB)
        self.assertLess(rates["after0"], 60 * KB)

    def test_one_block_for_every_send_shares_the_cap_evenly_and_holds_them(self):
        """The counterpart: the same three sends, every one asking for the
        same block - the new one, as each does from its next pass on after a
        rehash - move an even split, and each is held."""
        for cap_kb, _old_block, new_block in self.MIXED:
            with self.subTest(MAX_UPLOAD_KB=cap_kb, block=new_block):
                self.set_config(MAX_UPLOAD_KB=cap_kb)
                sends = [ChunkSender(f"send{n}", self.FAST_LINK, self.START, blocks=[new_block])
                         for n in range(3)]
                rates = self.rates_of(sends)
                total = sum(rates.values())
                self.assertTrue(dcc.upload_cap_binds(total), rates)
                even = dcc.SLOW_SEND_CAP_NEAR * total / len(sends)
                for send in sends:
                    self.assertGreaterEqual(rates[send.name], even, rates)
                self.assertTrue(all(self.held(sends, rates).values()), rates)

    def blocks_the_send_loop_asks_for(self, start_block, changed_to, passes):
        """The block stream_the_file() passes to chunk_size() on each pass of
        a send started at `start_block` whose DCC_BLOCK_SIZE is changed to
        `changed_to` after its first chunk - read off the real loop, with no
        cap so nothing waits."""
        tmp = tempfile.mkdtemp(prefix="dccore-blocks-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "Some_Album.flac")
        with io.open(path, "wb") as handle:
            handle.write(bytes(start_block + changed_to * passes))
        row = {"user": SENDER, "file": "Some_Album.flac", "bytes_sent": 0}
        self.set_config(DCC_BLOCK_SIZE=start_block, MAX_UPLOAD_KB=0, SLOW_SEND_MIN_KB=0,
                        active_transfers=[row])
        asked = []
        real_chunk_size = speed_cap.chunk_size

        def recording(direction, block):
            asked.append(block)
            return real_chunk_size(direction, block)

        def changing(conn, tracker, wait=0.0):
            acking_everything(conn, tracker, wait)
            if conn.chunks == 1:
                config.DCC_BLOCK_SIZE = changed_to

        conn = OneSecondPerChunk(FakeClock())
        with mock.patch.object(speed_cap, "chunk_size", recording), \
                mock.patch.object(dcc, "_drain_acks", changing):
            dcc.stream_the_file(conn, path, 0, 0, dcc._AckTracker(), row, SENDER)
        return asked

    def test_the_blocks_the_send_loop_asks_for_after_a_change_share_the_cap_evenly(self):
        """The two tied together: the send started before the rehash is fed
        the blocks the real stream_the_file() asks for when DCC_BLOCK_SIZE
        goes from 64 KB to 8 KB after its first chunk, beside two sends
        started after it at 8 KB. It asks for 64 KB once and 8 KB from then
        on, so over the watch's window the three split the cap evenly and
        every one is held. Read once per transfer, it asked for 64 KB every
        pass, and the other two moved 57 KB/s each, unheld."""
        asked = self.blocks_the_send_loop_asks_for(64 * KB, 8 * KB, passes=32)
        self.assertEqual(asked[0], 64 * KB)
        self.assertEqual(set(asked[1:]), {8 * KB}, "every pass after the change asks for the new block")

        self.set_config(MAX_UPLOAD_KB=400, DCC_BLOCK_SIZE=8 * KB)
        before = ChunkSender("before", self.FAST_LINK, self.START, blocks=asked)
        after = [ChunkSender(f"after{n}", self.FAST_LINK, self.START) for n in range(2)]
        sends = [before] + after
        rates = self.rates_of(sends)
        total = sum(rates.values())
        self.assertTrue(dcc.upload_cap_binds(total), rates)
        even = dcc.SLOW_SEND_CAP_NEAR * total / len(sends)
        for send in sends:
            self.assertGreaterEqual(rates[send.name], even, rates)
        self.assertEqual(self.held(sends, rates), {s.name: True for s in sends}, rates)


def acking_everything(conn, tracker, wait=0.0):
    """A receiver that acknowledges all it has been sent, at once."""
    tracker.feed(struct.pack("!I", tracker.sent & 0xFFFFFFFF))


class OneSecondPerChunk:
    """A connection on which every chunk takes one second of `clock`."""

    def __init__(self, clock):
        self.clock = clock
        self.chunks = 0
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.chunks += 1
        self.clock.t += 1.0

    def close(self):
        pass


BLOCK = 4096
CHUNKS = 100
CONTENT = bytes(range(256)) * (BLOCK // 256) * CHUNKS     # 409,600 bytes, 100 blocks


class TheSendLoop(WithEverySlotTaken):
    """stream_the_file() with a 4 KB block and a second per chunk: 4.0 KB/s,
    beside OTHER's send delivering 100 KB/s."""

    def setUp(self):
        super().setUp()
        self.set_config(DCC_BLOCK_SIZE=BLOCK)
        tmp = tempfile.mkdtemp(prefix="dccore-crawl-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.path = os.path.join(tmp, "Some_Album.flac")
        with io.open(self.path, "wb") as handle:
            handle.write(CONTENT)
        self.clock = FakeClock()

    def stream(self):
        conn = OneSecondPerChunk(self.clock)
        acks = dcc._AckTracker()
        with mock.patch.object(dcc, "_slow_send_clock", self.clock), \
                mock.patch.object(dcc, "_drain_acks", acking_everything), \
                mock.patch.object(acks, "stalled", return_value=False):
            try:
                return dcc.stream_the_file(conn, self.path, 0, 0, acks, self.mine, SENDER), conn, acks
            except dcc._TooSlowWhileOthersWait as raised:
                return raised, conn, acks

    def test_a_crawl_under_the_floor_is_stopped_at_one_minute(self):
        self.set_config(SLOW_SEND_MIN_KB=5)
        raised, conn, acks = self.stream()
        self.assertIsInstance(raised, dcc._TooSlowWhileOthersWait)
        self.assertEqual(conn.chunks, 60, "stopped at the first check a minute in")
        self.assertEqual(raised.speed, 4.0 * KB)
        self.assertEqual(raised.acked, 60 * BLOCK)
        self.assertEqual(acks.sent, 60 * BLOCK)

    def test_one_over_the_floor_runs_to_the_end_and_publishes_its_rate(self):
        self.set_config(SLOW_SEND_MIN_KB=3)
        sent, conn, _acks = self.stream()
        self.assertEqual(sent, len(CONTENT))
        self.assertEqual(conn.chunks, CHUNKS)
        self.assertEqual(self.mine.get(RATE), 4.0 * KB, "the send's row carries its rate")

    def test_with_a_slot_free_the_crawl_runs_to_the_end(self):
        self.set_config(SLOW_SEND_MIN_KB=5, MAX_DCC_SLOTS=3)
        sent, conn, _acks = self.stream()
        self.assertEqual(sent, len(CONTENT))


class Recorder:
    """threading.Thread for finish_send()'s queue wake-up: kept, not started,
    so the delay it was given can be read."""

    made = []

    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target
        Recorder.made.append(self)

    def start(self):
        pass

    def delay(self):
        cells = dict(zip(self.target.__code__.co_freevars,
                         (cell.cell_contents for cell in self.target.__closure__)))
        return cells["retry_delay"]


class TheEnding(WithEverySlotTaken):
    """finish_send() with SENDER's real queue row."""

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        self.set_config(SLOW_SEND_MIN_KB=5)
        self.queued_row = {"file": "Some_Album.flac", "user": SENDER, "path": os.path.join("nowhere", "Some_Album.flac")}
        config.dcc_queue[SENDER] = [self.queued_row, {"file": "Next_Album.flac", "user": SENDER}]
        runtime.queue_waiting_since[SENDER] = 1.0
        config.user_processing_lock.add(SENDER)
        self.addCleanup(config.user_processing_lock.discard, SENDER)
        for name, value in (("_record_send_outcome", lambda *a, **k: None),
                            ("check_queue_and_send", lambda *a, **k: None)):
            patch = mock.patch.object(dcc, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        Recorder.made = []

    def finish(self, **fields):
        sent = dict(
            row=self.mine, user=SENDER, next_file=self.queued_row,
            file_path=self.queued_row["path"], file_name="Some_Album.flac",
            file_size=4_000_000, resume_offset=0, bytes_sent=245_760,
            transfer_completed=False, already_held=False, offered_at=100.0, start_time=100.0,
            transfer_finished_at=160.0, channel=CHANNEL, irc_sock=object(),
            owns_packer=False, outcome_identity=("k", "Some_Album.flac", "file"),
            outcome_status="failed", acked=245_760, assigned_port=5001)
        sent.update(fields)
        made_before = len(Recorder.made)
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(threading, "Thread", Recorder):
            dcc.finish_send(dcc.SentFile(**sent))
        self.assertEqual(len(Recorder.made), made_before + 1, "the queue is woken once")
        return Recorder.made[-1].delay()

    def notices(self):
        return [line for _nick, line, _vip in self.oserve.queued]

    def test_the_row_is_kept_charged_and_its_nick_told_and_sent_to_the_back(self):
        delay = self.finish(too_slow_bps=2048.0)

        self.assertIn(self.queued_row, config.dcc_queue[SENDER], "the row stays queued")
        self.assertEqual(self.queued_row["send_fails"], 1, "charged like a stall")
        notices = self.notices()
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("Stopped", notices[0])
        self.assertIn("stays in your queue", notices[0])
        self.assertNotIn("Could not send", notices[0])
        self.assertGreater(runtime.queue_waiting_since[SENDER], 1.0, "the nick went to the back")
        self.assertEqual(delay, 3, "the freed slot goes to the next in line at once")

    def test_the_kept_row_is_marked_and_the_mark_is_saved_and_loaded(self):
        self.finish(too_slow_bps=2048.0)

        self.assertIs(self.queued_row.get(MARK), True)
        with io.open(db.DCC_QUEUE_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertIs(saved[SENDER][0].get(MARK), True, "the mark is in dcc_queue.txt")
        self.assertNotIn(MARK, saved[SENDER][1], "only the row that was stopped")
        config.dcc_queue.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            db.load_dcc_queue()
        self.assertIs(config.dcc_queue[SENDER][0].get(MARK), True, "and survives a restart")

    def test_a_plain_failure_of_the_same_row_backs_off_and_is_not_marked(self):
        delay = self.finish()

        self.assertEqual(self.queued_row["send_fails"], 1)
        self.assertNotIn(MARK, self.queued_row)
        self.assertEqual(self.notices(), [], "a kept row is not told of a plain failure")
        self.assertEqual(delay, 15)

    def test_the_last_try_drops_the_row_with_the_reason_in_plain_words(self):
        self.queued_row["send_fails"] = config.MAX_SEND_FAILS - 1

        self.finish(too_slow_bps=2048.0)

        self.assertNotIn(self.queued_row, config.dcc_queue.get(SENDER, []))
        notices = self.notices()
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("Could not send Some_Album.flac", notices[0])
        self.assertIn(dcc.too_slow_reason(2048.0, to_the_user=True), notices[0])
        self.assertNotIn("SLOW_SEND_MIN_KB", notices[0])
        self.assertFalse(any("Stopped" in line for line in notices))

    def test_a_direct_send_is_dropped_and_told_to_ask_again(self):
        """A send that was never queued (the free-slot fast path) has no row
        to keep: it is dropped, said why, and not told it stays queued."""
        direct = {"file": "Loose_Track.flac", "user": SENDER}

        self.finish(too_slow_bps=2048.0, next_file=direct, file_name="Loose_Track.flac")

        notices = self.notices()
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("Could not send Loose_Track.flac - "
                      + dcc.too_slow_reason(2048.0, to_the_user=True), notices[0])
        self.assertIn("Ask for it again when you are ready.", notices[0])
        self.assertFalse(any("Stopped" in line for line in notices))
        self.assertEqual(config.dcc_queue[SENDER][0], self.queued_row, "the real queue is untouched")

    def test_two_crawlers_do_not_trade_the_slot(self):
        """SENDER crawls and is stopped for WAITER; WAITER gets the slot and
        crawls too. With only SENDER - stopped for speed - waiting, WAITER is
        not stopped to hand the slot back."""
        clock = FakeClock()
        watch = dcc._SlowSendWatch(0, clock=clock, row=self.mine)
        acked = 0
        for _ in range(60):
            clock.t += 1.0
            acked += KB
            speed = watch.check(acked)
        self.assertEqual(speed, 1.0 * KB, "the first crawler is stopped")
        self.finish(too_slow_bps=speed)
        self.assertNotIn(self.mine, config.active_transfers)

        theirs = {"user": WAITER, "file": "Another_Album.flac", "bytes_sent": 0}
        config.active_transfers.append(theirs)
        watch = dcc._SlowSendWatch(0, clock=clock, row=theirs)
        acked = 0
        for _ in range(300):
            clock.t += 1.0
            acked += KB
            self.assertIsNone(watch.check(acked), "the slot went back to the nick just stopped")


class _AReceiverThatCrawls:
    """socket.socket for a listener whose one receiver connects at once and
    takes a second of `clock` per chunk."""

    clock = None

    def __init__(self, *args, **kwargs):
        pass

    def setsockopt(self, *args):
        pass

    def bind(self, address):
        pass

    def settimeout(self, value):
        pass

    def listen(self, backlog):
        pass

    def accept(self):
        return OneSecondPerChunk(_AReceiverThatCrawls.clock), ("198.51.100.23", 50123)

    def close(self):
        pass


class TheWholeSend(WithEverySlotTaken):
    """start_dcc_send() from the offer to finish_send(), with the receiver
    replaced by one that crawls at 4.0 KB/s under a 5 KB/s floor."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp(prefix="dccore-crawl-send-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.served = os.path.join(tmp, "Some_Album.flac")
        with io.open(self.served, "wb") as handle:
            handle.write(CONTENT)
        self.set_config(SLOW_SEND_MIN_KB=5, DCC_BLOCK_SIZE=BLOCK, MY_IP_OR_DOCK="8.8.8.8",
                        DCC_PORT_START=PORT_START, DCC_PORT_END=PORT_END)
        self.queued_row = {"file": "Some_Album.flac", "user": SENDER, "path": self.served}
        config.dcc_queue[SENDER] = [self.queued_row]
        config.user_processing_lock.add(SENDER)
        self.addCleanup(config.user_processing_lock.discard, SENDER)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        self.events = []
        self.clock = FakeClock()
        _AReceiverThatCrawls.clock = self.clock
        self._patch(announce, "feed_event", lambda kind, text, **fields: self.events.append((kind, text, fields)))
        self._patch(socket, "socket", _AReceiverThatCrawls)
        self._patch(dcc, "_slow_send_clock", self.clock)
        self._patch(dcc, "_drain_acks", acking_everything)
        self._patch(dcc._AckTracker, "stalled", lambda tracker, *a, **k: False)
        self.oserve.send_fails_count = 0

    def _patch(self, target, name, value):
        patch = mock.patch.object(target, name, value)
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_is_reported_charged_and_told_as_a_crawl(self):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.start_dcc_send(irc, SENDER, self.served, "Some_Album.flac", CHANNEL, self.queued_row)

        failures = [(text, fields) for kind, text, fields in self.events if kind == "FAIL"]
        self.assertEqual(len(failures), 1, self.events)
        text, fields = failures[0]
        self.assertIn("sent at 4.0 KB/s, under SLOW_SEND_MIN_KB (5 KB/s) while others waited "
                      f"for a slot; stopped at {60 * BLOCK:,} of {len(CONTENT):,} bytes", text)
        self.assertEqual(fields.get("acked"), 60 * BLOCK)
        self.assertEqual(fields.get("total"), len(CONTENT))
        self.assertEqual(self.oserve.send_fails_count, 1)
        # finish_send() was handed the rate: the row is kept, marked and the nick told.
        self.assertIn(self.queued_row, config.dcc_queue[SENDER])
        self.assertEqual(self.queued_row["send_fails"], 1)
        self.assertIs(self.queued_row.get(MARK), True)
        told = [line for nick, line, _vip in self.oserve.queued if nick == SENDER]
        self.assertTrue(any("stays in your queue" in line for line in told), told)
        self.assertNotIn(self.mine, config.active_transfers, "the slot is freed")


if __name__ == "__main__":
    unittest.main()
