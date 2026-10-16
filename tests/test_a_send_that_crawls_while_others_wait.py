"""#1388: a send that crawls while every slot is taken and others wait is stopped.

SLOW_SEND_MIN_KB, 0 (off) by default. Above 0, a send whose receiver has
acknowledged less than that over about the last minute is stopped - judged
from a minute after it connected, only while every slot is taken and a nick
is waiting for one, and never while the bot's own upload cap (#1352) is what
holds the sends back. The row is kept and charged against MAX_SEND_FAILS like
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
a fake listener whose receiver connects and crawls.

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

    def test_not_while_the_upload_cap_is_what_holds_the_sends_back(self):
        self.set_config(MAX_UPLOAD_KB=100)
        self.assertIsNone(self.drive(300, kb_per_second=1, uploaded_per_second=0.95 * 100 * KB))

    def test_a_cap_the_sends_are_nowhere_near_does_not_shield_it(self):
        self.set_config(MAX_UPLOAD_KB=100)
        self.assertIsNotNone(self.drive(120, kb_per_second=1, uploaded_per_second=0.2 * 100 * KB))


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
