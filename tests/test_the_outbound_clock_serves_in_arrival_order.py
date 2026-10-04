"""The shared outbound clock had no ordering (audit M53, #655).

OutboundPacer.wait_for_slot() was sleep-and-retry with no queue: every
waiter slept until the same instant and whoever woke first took the slot.
Four threads share it - queue_worker's VIP and standard lanes, the debug
drain, the !ping and DCC ACCEPT direct waiters - so queue_worker's strict
alternation bounded VIP to two of its OWN slots while the worker lost each
of those to the drain by coin toss. Measured with the real threads: a
drain backlog gave VIP about a quarter of the slots and gaps of ten to
fourteen slots - a minute at MSG_DELAY=5 - between consecutive VIP lines,
long enough to push a "Sending:" notice past the receiver's accept window.

Tickets now: handed out in arrival order, served in that order. Only the
ticket being served sleeps against the clock; the rest wait to be woken.
A waiter that leaves without its slot is stepped over. The combined rate is
what it was (tests/test_a_shared_outbound_pace.py keeps that); what changes
is who gets the next slot, which is now whoever asked first.
"""

import collections
import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import runtime  # noqa: E402


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _TheClocksOwnRecord:
    """The pacer's Condition, recording what happens under its own lock.

    Every wait_for_slot() takes its ticket as the first thing it does inside
    `with self._cond`, and the caller that is served calls notify_all()
    while it still holds the lock. Logged right there, the order of the
    entries is the order tickets were handed out, and the order of the
    notifications is the order slots were served - not the order the lanes
    got round to writing anything down afterwards."""

    def __init__(self, cond):
        self._cond = cond
        self.asked = []
        self.served = []

    def __enter__(self):
        entered = self._cond.__enter__()
        self.asked.append(threading.current_thread().name)
        return entered

    def __exit__(self, *exc):
        return self._cond.__exit__(*exc)

    def wait(self, timeout=None):
        return self._cond.wait(timeout)

    def notify_all(self):
        self.served.append(threading.current_thread().name)
        self._cond.notify_all()


class ThreeLanesHammeringIt(unittest.TestCase):
    """The audit's own probe: three threads looping on a fresh clock. It
    measured a longest streak of 11 slots without one of them; in arrival
    order no lane can be skipped while it is waiting.

    It used to assert that on the order the lanes appended to a list after
    each slot - no lane three times in a row - and failed 4 runs in 5 on a
    loaded machine while the clock was fair. Logged under the clock's own
    lock, those runs served every slot in exactly the order its ticket was
    taken. A lane that wins, then loses the CPU before it asks again, is not
    waiting: the other two may rightly take the next slots without it. The
    streak measured the scheduler. What is asserted now is the property
    itself, and it holds however the threads are scheduled."""

    def test_nobody_is_skipped_while_waiting(self):
        pacer = runtime.OutboundPacer()
        record = pacer._cond = _TheClocksOwnRecord(pacer._cond)
        wins, lock = [], threading.Lock()

        def hammer(count=40):
            for _ in range(count):
                pacer.wait_for_slot(0.004)
                with lock:
                    wins.append(threading.current_thread().name)

        threads = [threading.Thread(target=hammer, name="lane%d" % i) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive())

        self.assertEqual(collections.Counter(wins), {"lane0": 40, "lane1": 40, "lane2": 40})
        self.assertEqual(len(record.served), 120, "a slot was given without being recorded")
        # Served in the order asked: a lane holding a ticket is never passed over.
        self.assertEqual(record.served, record.asked)


class ArrivalOrderIsServiceOrder(unittest.TestCase):
    """Deterministic: the clock is held, three waiters queue in a known
    order, the clock is released, and they are served in that order."""

    def setUp(self):
        self.pacer = runtime.OutboundPacer()
        self.served = []
        self.lock = threading.Lock()
        self.threads = []

    def tearDown(self):
        self.release()
        for thread in self.threads:
            thread.join(5)

    def hold_the_clock(self):
        self.pacer.wait_for_slot(3600.0)   # the next slot is an hour away

    def release(self):
        with self.pacer._cond:
            self.pacer._next_allowed = 0.0
            self.pacer._cond.notify_all()

    def queue(self, name):
        """Start a waiter and return once it holds its ticket."""
        before = self.pacer._next_ticket

        def run():
            self.pacer.wait_for_slot(0.0)
            with self.lock:
                self.served.append(name)
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(wait_until(lambda: self.pacer._next_ticket == before + 1), "the waiter never queued")

    def test_first_come_first_served(self):
        self.hold_the_clock()
        for name in ("drain", "vip", "ping", "standard"):
            self.queue(name)
        self.assertEqual(self.served, [], "nothing may be served while the clock is held")

        self.release()

        self.assertTrue(wait_until(lambda: len(self.served) == 4), self.served)
        self.assertEqual(self.served, ["drain", "vip", "ping", "standard"])

    def test_a_waiter_that_leaves_does_not_block_the_line(self):
        """The thread behind a ticket can die (a worker being torn down).
        Its ticket is stepped over, not waited on for ever."""
        self.hold_the_clock()
        self.queue("first")

        leaver_started = threading.Event()
        leaver_error = []
        real_wait = self.pacer._cond.wait

        class Leaves(RuntimeError):
            pass

        def wait_or_leave(timeout=None):
            if threading.current_thread().name == "leaver":
                raise Leaves("torn down while queued")
            return real_wait(timeout)
        self.pacer._cond.wait = wait_or_leave
        self.addCleanup(setattr, self.pacer._cond, "wait", real_wait)

        def leave():
            leaver_started.set()
            try:
                self.pacer.wait_for_slot(0.0)
            except Leaves as err:
                leaver_error.append(err)
        leaver = threading.Thread(target=leave, name="leaver", daemon=True)
        self.threads.append(leaver)
        leaver.start()
        leaver.join(5)
        self.assertEqual(len(leaver_error), 1, "the leaver did not leave")
        self.pacer._cond.wait = real_wait

        self.queue("third")
        self.release()

        self.assertTrue(wait_until(lambda: len(self.served) == 2),
                        "the line stopped at the abandoned ticket: %r" % self.served)
        self.assertEqual(self.served, ["first", "third"])


class TheClockIsStillTheClock(unittest.TestCase):
    """Ordering did not loosen the rate: two slots asked for back to back
    are still one interval apart."""

    def test_two_slots_are_one_interval_apart(self):
        pacer = runtime.OutboundPacer()
        pacer.wait_for_slot(0.05)
        started = time.monotonic()
        pacer.wait_for_slot(0.05)

        self.assertGreaterEqual(time.monotonic() - started, 0.045)

    def test_the_first_slot_is_free_at_once(self):
        pacer = runtime.OutboundPacer()
        started = time.monotonic()
        pacer.wait_for_slot(5.0)

        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
