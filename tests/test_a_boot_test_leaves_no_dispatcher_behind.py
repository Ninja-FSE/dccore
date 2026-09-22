"""Boot tests left a live fetch_dispatcher_worker thread in the process
(#799).

tests/test_startup.py's BootCase stubbed queue_mgr.queue_worker so
oserve.startup() does not leave a live pump per test - but not
dcc_fetch.fetch_dispatcher_worker, which startup() starts eleven lines
later. Only the subclass that tests the dispatcher stubbed it, so every
other boot left a real `while True` thread calling check_fetch_queue()
every 2 s for the rest of the suite, through whichever oserve stub a later
test had installed. On a slow runner the tick landed between a test's
paste and its pause, and a test that asserts nothing is dispatched while
transfers are paused found three requests already sent. Twice.

Both fixtures now stub it. This drives one real boot the way BootCase does
and asserts no thread is running the real dispatcher afterwards - the
property, not the fixture's shape.
"""

import io
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_startup import BootCase  # noqa: E402


def threads_running(target):
    """Threads whose target is `target`. Thread keeps it as _target until
    run() returns; a loop that never returns keeps it for ever."""
    return [t for t in threading.enumerate()
            if getattr(t, "_target", None) is target and t.is_alive()]


class ABootThroughTheFixture(BootCase):

    def test_leaves_no_real_dispatcher_running(self):
        before = threads_running(self.real_dispatcher())
        self.boot()

        self.assertEqual(len(self._wait_for_dispatchers()), 1, "the boot did not start it at all")
        self.assertEqual(threads_running(self.real_dispatcher()), before,
                         "a real fetch_dispatcher_worker thread is running after a stubbed boot")

    def real_dispatcher(self):
        return self._real_dispatcher

    def _wait_for_dispatchers(self):
        for _ in range(200):
            if self.dispatchers:
                break
            threading.Event().wait(0.01)
        return self.dispatchers


class NoBootLeftOneBehindBeforeThisRan(DCCoreTestCase):
    """The suite-wide property: by the time this runs, however many boot
    tests came before it (test_startup sorts after this file, so on a full
    run it is the browser-setup boot and the one above), none of them left
    the real loop alive."""

    def test_no_real_dispatcher_thread_is_alive(self):
        alive = threads_running(dcc_fetch.fetch_dispatcher_worker)

        self.assertEqual(alive, [], "a boot test leaked the fetch dispatcher: %r" % alive)


if __name__ == "__main__":
    unittest.main()
