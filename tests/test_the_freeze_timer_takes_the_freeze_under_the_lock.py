"""The freeze timer tested the freeze outside queue_lock and erased inside
it without looking again (audit M57, #659).

user_queue_timer's expiry read `t_key in config.frozen_queues` unlocked,
then under queue_lock deleted the user's queue and did an unconditional
`del config.frozen_queues[t_key]`. The JOIN thaw (an unlocked pop) and the
sweep thaw (under the lock, which also widens the window to however long
the sweep holds it) both remove that key. One landing in the gap - the user
back at the 300 s mark - meant the timer erased the queue of someone who
was verifiably present and then died on the KeyError: the freezer
destroying the queue it exists to preserve, with no "Timer expired" line
to say so. Rare, and silent when it hit.

The freeze is now tested and taken under the lock in one pop(), and only a
real answer erases anything. Driven here with the race made deterministic:
the test holds queue_lock, waits until the countdown is blocked on it,
thaws the user, and lets go.
"""

import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests import test_the_freeze_box_has_one_clock as clock  # noqa: E402


class _AnnouncingLock:
    """dcc.queue_lock with a flag raised the moment somebody tries to take it."""

    def __init__(self, real):
        self.real = real
        self.wanted = threading.Event()

    def __enter__(self):
        self.wanted.set()
        return self.real.__enter__()

    def __exit__(self, *exc):
        return self.real.__exit__(*exc)


class AThawAsTheCountdownEnds(clock.TheTimerThreadReadsTheSameClock):
    """The countdown fixture from the one-clock tests (parked sleep, a
    caught timer thread), plus the lock made observable."""

    def setUp(self):
        super().setUp()
        self.real_lock = dcc.queue_lock
        self.lock = _AnnouncingLock(self.real_lock)
        dcc.queue_lock = self.lock
        self.addCleanup(setattr, dcc, "queue_lock", self.real_lock)
        self.errors = []
        real_hook = threading.excepthook
        threading.excepthook = lambda args: self.errors.append(args.exc_value)
        self.addCleanup(setattr, threading, "excepthook", real_hook)

    def run_countdown_into_a_thaw(self, thaw):
        self.start_countdown()
        with self.real_lock:
            config.frozen_queues["dave"] = time.time() - (dcc.FREEZE_TIMEOUT + 5)
        # Hold the lock ourselves; let the countdown finish its last pass and
        # arrive at the expiry, where it must wait for us.
        with self.real_lock:
            self.lock.wanted.clear()
            self.go.set()
            self.assertTrue(self.lock.wanted.wait(10), "the countdown never reached the expiry")
            thaw()                      # the user is back, in the gap
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())

    def test_a_join_thaw_in_the_gap_keeps_the_queue(self):
        """irc.thaw_one_user() is the JOIN handler's unlocked pop."""
        self.run_countdown_into_a_thaw(lambda: self.assertTrue(irc.thaw_one_user("dave")))

        self.assertEqual(self.errors, [], "the countdown thread died")
        self.assertIn("dave", config.dcc_queue, "the timer erased the queue of a user who was back")
        self.assertNotIn("dave", config.frozen_queues)

    def test_a_sweep_thaw_in_the_gap_keeps_the_queue(self):
        """The sweep's thaw pops under the lock it already holds - here, ours."""
        self.run_countdown_into_a_thaw(lambda: config.frozen_queues.pop("dave", None))

        self.assertEqual(self.errors, [])
        self.assertIn("dave", config.dcc_queue)

    def test_with_no_thaw_the_expiry_still_erases(self):
        """The control: the same steps with nobody thawing."""
        self.run_countdown_into_a_thaw(lambda: None)

        self.assertEqual(self.errors, [])
        self.assertNotIn("dave", config.dcc_queue)
        self.assertNotIn("dave", config.frozen_queues)


# The inherited case ran in its own module.
for _name in [n for n in dir(clock.TheTimerThreadReadsTheSameClock) if n.startswith("test")]:
    setattr(AThawAsTheCountdownEnds, _name, None)


class TheShapeOfTheExpiry(unittest.TestCase):

    def test_the_freeze_is_popped_under_the_lock_and_nothing_is_deleted_otherwise(self):
        import inspect
        body = inspect.getsource(dcc.freeze_absent_user)
        expiry = body.split("still_frozen = False", 1)[1]

        self.assertIn("with queue_lock:", expiry.split("frozen.pop(t_key, None)", 1)[0])
        self.assertIn("if still_frozen and t_key in config.dcc_queue:", expiry)
        self.assertNotIn("del config.frozen_queues[t_key]", expiry)


if __name__ == "__main__":
    unittest.main()
