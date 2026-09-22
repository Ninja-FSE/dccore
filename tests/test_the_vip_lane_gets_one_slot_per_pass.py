"""#527: "when you get spammed with requests, the Sent: doesn't send to the
channels before the queue is empty - while channel announce works and debug
messages."

queue_mgr.queue_worker() drained one VIP line per pass and then one line
for EVERY user with a backlog. Each line costs one MSG_DELAY slot on the
shared pacer - five seconds by default - so with N users spamming, the VIP
lane (Sent:, the advert, "Sending:") got one slot in N+1. The debug drain
has its own thread and its own share of the pacer, which is why debug lines
still came through.

That was #426 over-corrected: it removed the `continue` that let VIP starve
the standard lane completely, and what replaced it starved VIP instead.

Now: one VIP line, one standard line, per pass, with the standard lane
rotating through users across passes via a cursor. VIP is never more than
two slots away; standard users are still served in turn, over time instead
of all within one pass.
"""

import contextlib
import io
import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import queue_mgr  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_a_shared_outbound_pace import _SleepShim, TimestampedSocket  # noqa: E402


class TheCursorRotatesThroughUsers(unittest.TestCase):
    """next_standard_line() - the pure part."""

    def test_nothing_queued(self):
        self.assertIsNone(queue_mgr.next_standard_line({}, None))

    def test_the_first_call_serves_the_first_user(self):
        queue = {"a": ["a1"], "b": ["b1"]}

        self.assertEqual(queue_mgr.next_standard_line(queue, None), ("a", "a1"))

    def test_the_next_call_serves_the_user_after_the_last_one(self):
        queue = {"a": ["a1", "a2"], "b": ["b1"], "c": ["c1"]}

        served = []
        last = None
        for _ in range(4):
            user, _line = queue_mgr.next_standard_line(queue, last)
            served.append(user)
            last = user

        self.assertEqual(served, ["a", "b", "c", "a"])

    def test_it_wraps_round(self):
        queue = {"a": ["a1"], "b": ["b1"]}

        self.assertEqual(queue_mgr.next_standard_line(queue, "b"), ("a", "a1"))

    def test_a_user_who_has_gone_means_start_from_the_top(self):
        queue = {"b": ["b1"], "c": ["c1"]}

        self.assertEqual(queue_mgr.next_standard_line(queue, "a"), ("b", "b1"))

    def test_a_user_served_to_empty_keeps_their_place_until_the_cursor_passes(self):
        """The first draft deleted them on the spot - and the cursor, being
        their name, then had nowhere to stand: the next call started from
        the top, and with {a: 2 lines, b: 1, c: 1} the order came out
        a b a c. Caught by the rotation test above."""
        queue = {"a": ["a1"], "b": ["b1"]}

        queue_mgr.next_standard_line(queue, None)

        self.assertEqual(queue, {"a": [], "b": ["b1"]},
                         "the emptied user must hold the cursor's place")

        self.assertEqual(queue_mgr.next_standard_line(queue, "a"), ("b", "b1"))
        self.assertIsNone(queue_mgr.next_standard_line(queue, "b"),
                          "nothing left, so nothing served")
        self.assertEqual(queue, {}, "the wrap tidies both empty entries away")

    def test_a_user_with_lines_left_stays(self):
        queue = {"a": ["a1", "a2"]}

        queue_mgr.next_standard_line(queue, None)

        self.assertEqual(queue, {"a": ["a2"]})

    def test_an_empty_entry_is_tidied_on_the_way_past(self):
        """oserve.queue_message() can leave a key with an empty list; the
        old loop deleted those as it met them, and so does this."""
        queue = {"a": [], "b": ["b1"]}

        self.assertEqual(queue_mgr.next_standard_line(queue, None), ("b", "b1"))
        self.assertNotIn("a", queue)

    def test_only_empty_entries_means_nothing(self):
        queue = {"a": [], "b": []}

        self.assertIsNone(queue_mgr.next_standard_line(queue, None))
        self.assertEqual(queue, {})

    def test_lines_leave_in_order(self):
        queue = {"a": ["a1", "a2", "a3"]}

        lines = [queue_mgr.next_standard_line(queue, None)[1] for _ in range(3)]

        self.assertEqual(lines, ["a1", "a2", "a3"])


class _WorkerCase(DCCoreTestCase):
    """Runs the real queue_worker thread - the same fixture shape as
    tests/test_a_shared_outbound_pace.py, whose shim and socket it borrows."""

    def setUp(self):
        super().setUp()
        self.set_config(MSG_DELAY=0.01)
        config.vip_queue = []
        config.send_queue = {}
        config.bot_joined_channel = True
        self.oserve.bot_joined_channel = True
        # The pump also waits for activation (#630); the harness resets this.
        config.activation_triggered = True
        runtime.outbound_pacer = runtime.OutboundPacer()
        self.sock = TimestampedSocket()
        self.oserve.irc_connection = self.sock
        self._real_queue_time = queue_mgr.time
        self.queue_shim = _SleepShim()
        queue_mgr.time = self.queue_shim
        self.thread = None

    def tearDown(self):
        self.queue_shim.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3.0)
        queue_mgr.time = self._real_queue_time
        runtime.outbound_pacer = runtime.OutboundPacer()
        super().tearDown()

    def start_worker(self):
        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    queue_mgr.queue_worker()
                except SystemExit:
                    pass
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def sent(self):
        return [payload.decode("utf-8") for _t, payload in self.sock.sent]

    def wait_for(self, predicate, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()


class VipIsNeverMoreThanTwoSlotsAway(_WorkerCase):
    """The report. Ten users with backlogs; a "Sent:" line arrives while
    the standard lane is mid-way. It must leave within two slots, not ten."""

    USERS = 10

    def setUp(self):
        super().setUp()
        for i in range(self.USERS):
            config.send_queue[f"user{i}"] = [f"NOTICE user{i} :reply {n}\r\n" for n in range(5)]

    def test_a_sent_line_arriving_under_load_goes_out_within_two_slots(self):
        self.start_worker()
        # Let the standard lane get going first, so the VIP line arrives
        # mid-pass rather than sitting at the top of the very first one -
        # where even the old code would have drained it first.
        self.assertTrue(self.wait_for(lambda: len(self.sock.sent) >= 1),
                        "the worker never sent anything")
        landed_at = len(self.sock.sent)

        config.vip_queue.append("PRIVMSG #chan :Sent: a file to somebody\r\n")

        self.assertTrue(self.wait_for(lambda: any("Sent:" in line for line in self.sent())),
                        "the Sent: line never went out at all")
        position = next(i for i, line in enumerate(self.sent()) if "Sent:" in line)
        waited = position - landed_at

        self.assertLessEqual(waited, 2,
                             f"the Sent: line waited {waited} slots behind "
                             f"standard-lane lines; with {self.USERS} users "
                             f"spamming that is the 1-in-N+1 share of #527")

    def test_the_standard_lane_still_runs_while_vip_has_a_backlog(self):
        """#426's property, kept: VIP never again starves the other lane."""
        for i in range(500):
            config.vip_queue.append(f"PRIVMSG #chan :vip {i}\r\n")

        self.start_worker()

        self.assertTrue(self.wait_for(lambda: any("NOTICE user" in line for line in self.sent()), 1.5),
                        "no standard-lane line went out while VIP had a backlog")


class TheStandardLaneIsStillFairAcrossPasses(_WorkerCase):
    """The cursor. Five users, three lines each, no VIP: every run of five
    consecutive sends must reach five different users. A cursor that always
    started from the top would give user0 every line until they ran dry."""

    def test_every_window_of_n_sends_serves_n_different_users(self):
        users = [f"user{i}" for i in range(5)]
        for user in users:
            config.send_queue[user] = [f"NOTICE {user} :line {n}\r\n" for n in range(3)]

        self.start_worker()

        self.assertTrue(self.wait_for(lambda: len(self.sock.sent) >= 15),
                        f"only {len(self.sock.sent)} of 15 lines went out")
        lines = self.sent()[:15]
        for window in range(3):
            chunk = lines[window * 5:(window + 1) * 5]
            served = {line.split()[1] for line in chunk}
            self.assertEqual(served, set(users),
                             f"sends {window * 5}-{window * 5 + 4} went to "
                             f"{sorted(served)} - a user was served twice "
                             f"before another was served once")

    def test_a_user_who_joins_the_queue_mid_way_is_reached(self):
        """The cursor is over a LIVE dict: a user added after the worker
        started must be served in turn, not skipped until a wrap."""
        config.send_queue["early"] = [f"NOTICE early :line {n}\r\n" for n in range(6)]
        self.start_worker()
        self.assertTrue(self.wait_for(lambda: len(self.sock.sent) >= 1))

        config.send_queue["late"] = ["NOTICE late :hello\r\n"]

        self.assertTrue(self.wait_for(lambda: any("NOTICE late" in line for line in self.sent())),
                        "the late user was never served")
        position = next(i for i, line in enumerate(self.sent()) if "NOTICE late" in line)
        self.assertLessEqual(position, 3,
                             "the late user waited behind the early user's "
                             "whole backlog rather than taking the next turn")


class WhatTheOldLoopDidIsWrittenDown(unittest.TestCase):
    """Not a test of the code - a test of the claim, so the arithmetic that
    justifies this change cannot quietly stop being true. With N users and
    MSG_DELAY 5 s, one pass was 1 + N slots; a Sent: line at the back of the
    VIP lane behind k others waited k passes."""

    def test_the_old_share_with_ten_spammers(self):
        users, msg_delay = 10, 5.0
        old_pass = (1 + users) * msg_delay
        new_pass = 2 * msg_delay

        self.assertEqual(old_pass, 55.0)
        self.assertEqual(new_pass, 10.0)
        self.assertGreater(old_pass / new_pass, 5,
                           "the old share is no longer far worse than the new; "
                           "this note is out of date")


if __name__ == "__main__":
    unittest.main()
