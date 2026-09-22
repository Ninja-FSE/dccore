"""Console heartbeat liveness was coupled to dcc.queue_lock and
db._disk_lock through status_lines() (audit L48, #712).

The finding: the timer burst is computed on the writer thread, which is
the only thread that drains the outbox, and status_lines() takes
dcc.queue_lock (stats_mgr.live_speed) and db._disk_lock
(load_advanced_stats_rolled); a long hold of either stalled every console
line, and after 90 s the script called the link dead and reconnected into
the same wall. The audit's probe held each lock for 2.3 s and read nothing.

This is #614 (audit M12), the same coupling by way of queue_lock, fixed
before this issue was filed: the figures are computed on a helper thread
with a deadline (STATUS_WAIT), and past it `DCCORE PING` stands in for the
burst while the writer goes on draining. test_status_slot_queue_and_pairing
covers queue_lock; this pins the disk lock the audit named alongside it,
with the audit's own probe.
"""

import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
# Through the module: a test class imported by name would be discovered
# and run a second time here.
from tests import test_status_slot_queue_and_pairing as pairing  # noqa: E402


class TheHeartbeatDoesNotWaitOnTheDiskLock(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, adminchat, "STATUS_WAIT", adminchat.STATUS_WAIT)
        adminchat.STATUS_WAIT = 0.2
        # runtime.disk_lock is db._disk_lock; held here, so the helper reading
        # the stats row cannot finish while the test holds it.
        self.assertTrue(runtime.disk_lock.acquire(timeout=3.0), "disk_lock is already held")
        self.addCleanup(runtime.disk_lock.release)

    def test_the_audits_probe_hears_a_ping_and_a_reply_while_the_lock_is_held(self):
        """The writer running, the disk lock taken, the timer due, then a
        command reply queued: the old writer sent nothing for as long as
        the lock was held; the audit read b'' for 2.3 s."""
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        s = adminchat.Session(a, "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = True
        s.structured = True
        s._status_sent_at = 0.0
        runtime.live_speed_sampled_at = 0.0
        s.start_writer()
        buffer = pairing.TheHeartbeatDoesNotWaitOnQueueLock.recv_until(self, b, b"DCCORE PING\n")
        self.assertIn(b"DCCORE PING\n", buffer, "nothing was heard: the writer is parked on the disk lock")
        s.send("a command reply")
        buffer += pairing.TheHeartbeatDoesNotWaitOnQueueLock.recv_until(self, b, b"a command reply\n")

        self.assertIn(b"DCCORE OUT a command reply\n", buffer)
        self.assertNotIn(b"DCCORE STATUS ", buffer, "no figures could be read while the lock was held")


if __name__ == "__main__":
    unittest.main()
