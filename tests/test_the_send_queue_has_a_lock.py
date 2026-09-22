"""config.send_queue had no lock (audit L1, #665).

The per-user text lanes are written by every request, search and reply
thread through oserve.queue_message() - `not in`, create, append - and
drained by the pump through queue_mgr.next_standard_line() - get, falsy,
pop the key. Neither held anything, so their correctness rested on where
CPython happens to check for a thread switch: unreachable on 3.11+, where
no switch can land inside either window, reachable on 3.10 (the documented
minimum) and on a free-threaded build. Where reachable: the line just
appended landed on a list the pump was dropping with its key and vanished,
or the pump popped the key between the producer's `not in` and its append
and the producer died on a KeyError - a search thread mid-results, say.

runtime.send_queue_lock now guards both (and the pump's per-user cap, which
also edits the lists), and the producer's three steps are one setdefault.
"""

import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import queue_mgr  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_the_search_header_goes_first import real_oserve  # noqa: E402


class _AnnouncingLock:
    """runtime.send_queue_lock with a flag raised when somebody wants it."""

    def __init__(self, real):
        self.real = real
        self.wanted = threading.Event()
        self.taken = 0

    def __enter__(self):
        self.wanted.set()
        self.real.__enter__()
        self.taken += 1
        return self

    def __exit__(self, *exc):
        return self.real.__exit__(*exc)


class BothSidesTakeTheLock(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.oserve = real_oserve()
        self.real_lock = runtime.send_queue_lock
        self.lock = _AnnouncingLock(self.real_lock)
        runtime.send_queue_lock = self.lock
        self.addCleanup(setattr, runtime, "send_queue_lock", self.real_lock)
        config.send_queue.clear()
        self.addCleanup(config.send_queue.clear)

    def test_the_lock_lives_in_runtime(self):
        """A !rehash reloads queue_mgr and oserve; a lock in either would be
        a fresh object per reload, and two threads would hold two locks."""
        self.assertIs(type(self.real_lock), type(threading.Lock()))

    def test_the_producer_takes_it(self):
        self.oserve.queue_message("dave", "NOTICE dave :hello\r\n")

        self.assertEqual(self.lock.taken, 1)
        self.assertEqual(config.send_queue["dave"], ["NOTICE dave :hello\r\n"])

    def test_the_pump_takes_it(self):
        config.send_queue["dave"] = ["NOTICE dave :one\r\n"]

        picked = queue_mgr.next_standard_line(config.send_queue, None)

        self.assertEqual(picked, ("dave", "NOTICE dave :one\r\n"))
        self.assertEqual(self.lock.taken, 1)

    def test_the_producer_waits_while_the_pump_holds_it(self):
        """The interleaving the audit described, made deterministic: the
        pump holds the lock and drops dave's emptied key; a request thread
        that arrives meanwhile must wait, then create the key afresh and
        land its line in the live dict - not on a list that is gone."""
        config.send_queue["dave"] = []          # drained: the pump is about to drop the key
        landed = threading.Event()

        def request_thread():
            self.oserve.queue_message("dave", "NOTICE dave :late\r\n")
            landed.set()

        with self.real_lock:                     # the pump, mid next_standard_line()
            producer = threading.Thread(target=request_thread, daemon=True)
            producer.start()
            self.assertTrue(self.lock.wanted.wait(5), "the producer never asked for the lock")
            self.assertFalse(landed.wait(0.2), "the producer wrote without the lock")
            config.send_queue.pop("dave", None)  # what the pump does to an emptied user

        self.assertTrue(landed.wait(5), "the producer never got the lock back")
        producer.join(5)
        self.assertEqual(config.send_queue.get("dave"), ["NOTICE dave :late\r\n"],
                         "the line was lost with the key")


class TheProducerIsOneStep(unittest.TestCase):

    def test_setdefault_not_test_create_append(self):
        import inspect
        source = inspect.getsource(real_oserve().queue_message)

        self.assertIn("send_queue.setdefault(user_key, []).append(message)", source)
        self.assertNotIn("if user_key not in", source)


if __name__ == "__main__":
    unittest.main()
