"""#605: nothing that touches a disk runs while queue_lock is held.

WHAT WAS WRONG

Two of the three dispatch paths - check_queue_and_send's section B and the
request path in handle_download_request - called announce's
send_dcc_sending_notice() from INSIDE `with queue_lock:`. Since #550 that
function stat()s the library path to fill the console feed's byte count, and
FILE_DIRECTORY is allowed to be an NFS mount. A stat on a hung mount blocks
forever, and every thread that takes queue_lock then blocks behind it:
queue_worker (the only outbound pump) samples live_speed() under it once a
second, so no NOTICE, advert or search reply left the bot at all, and the IRC
read thread takes it on every NICK line, so at the next nick change on the
network the PONGs stopped too and the server dropped the bot. The request
path also ran db.save_dcc_queue() - an fsync - under the same lock, so a slow
data/ disk serialised every request and dispatch behind it.

THE FIX

The claim (user_processing_lock.add and the active_transfers append) is what
the lock is for. The notice, the thread spawn and the save now run after the
lock is released, the way section A's plain-file branch always has.

THE TEST

Each hook below records whether dcc.queue_lock was held at the moment it was
called. These tests are single-threaded (thread creation is intercepted), so
a True can only mean the calling code itself was inside the lock.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
from tests.support import queue_row  # noqa: E402
from tests.test_path_security import InlineThread, PathSecurityBase  # noqa: E402

CHANNEL = "#dccore-test"


class RecordingThread(InlineThread):
    """InlineThread that also notes whether queue_lock was held at start()."""

    held = []

    def start(self):
        RecordingThread.held.append((getattr(self.target, "__name__", ""),
                                     dcc.queue_lock.locked()))
        super().start()


class LockHeldAtTheDiskCall(PathSecurityBase):

    def setUp(self):
        super().setUp()
        self.held = {}
        RecordingThread.held = []
        dcc.threading.Thread = RecordingThread

        def recorder(name):
            def hook(*args, **kwargs):
                self.held.setdefault(name, []).append(dcc.queue_lock.locked())
            return hook

        # PathSecurityBase already stubs these (and puts the originals back);
        # rebinding them here only swaps one stub for another.
        announce.send_dcc_sending_notice = recorder("sending")
        announce.send_dcc_queue_notice = recorder("queue")
        db.save_dcc_queue = recorder("save")

    def request(self, name, user="dave"):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.handle_download_request(self.sock, user, name, CHANNEL)

    def assert_not_under_the_lock(self, name):
        calls = self.held.get(name)
        self.assertTrue(calls, "%s was never called" % name)
        self.assertNotIn(True, calls, "%s ran while queue_lock was held" % name)

    def assert_thread_started_outside_the_lock(self, target):
        started = [held for name, held in RecordingThread.held if name == target]
        self.assertTrue(started, "%s was never started: %r" % (target, RecordingThread.held))
        self.assertNotIn(True, started, "%s was started while queue_lock was held" % target)

    # -- check_queue_and_send, section B ----------------------------------

    def test_a_promoted_user_is_notified_after_the_lock_is_released(self):
        config.channel_users[CHANNEL] = {"dave"}
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Waiting.flac")]

        with contextlib.redirect_stdout(io.StringIO()):
            dcc.check_queue_and_send(self.sock, "carl")

        self.assert_not_under_the_lock("sending")
        self.assert_thread_started_outside_the_lock("start_dcc_send")
        # The claim itself still happened, and still under the lock's cover:
        # the user is marked busy and the slot is taken before anyone else
        # can look.
        self.assertIn("dave", config.user_processing_lock)
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["dave"])
        self.assertFalse(dcc.queue_lock.locked(), "queue_lock leaked")

    def test_a_promotion_with_no_free_slot_still_returns_cleanly(self):
        """The early return inside the lock must leave nothing half-dispatched."""
        config.channel_users[CHANNEL] = {"dave"}
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Waiting.flac")]
        self.set_config(MAX_DCC_SLOTS=1)
        config.active_transfers.append({"user": "amy", "file": "Busy.flac",
                                        "bytes_sent": 0, "next_file_obj": "Busy.flac"})

        with contextlib.redirect_stdout(io.StringIO()):
            dcc.check_queue_and_send(self.sock, "carl")

        self.assertNotIn("sending", self.held)
        self.assertEqual(RecordingThread.held, [])
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertFalse(dcc.queue_lock.locked(), "queue_lock leaked")

    # -- handle_download_request -----------------------------------------

    def test_a_request_that_sends_at_once_is_notified_after_the_lock_is_released(self):
        self.request(os.path.basename(self.tree.tracks[0]))

        self.assert_not_under_the_lock("sending")
        self.assert_thread_started_outside_the_lock("start_dcc_send")
        self.assertIn("dave", config.user_processing_lock)
        self.assertEqual([tx["user"] for tx in config.active_transfers], ["dave"])
        self.assertFalse(dcc.queue_lock.locked(), "queue_lock leaked")

    def test_a_queued_request_is_saved_and_notified_after_the_lock_is_released(self):
        # A row already waiting puts the second request into the queue.
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="First.flac")]

        self.request(os.path.basename(self.tree.tracks[1]))

        self.assert_not_under_the_lock("save")
        self.assert_not_under_the_lock("queue")
        self.assertEqual([row["file"] for row in config.dcc_queue["dave"]],
                         ["First.flac", os.path.basename(self.tree.tracks[1])])
        self.assertEqual(RecordingThread.held, [], "a queued row must not dispatch")
        self.assertFalse(dcc.queue_lock.locked(), "queue_lock leaked")

    def test_a_pack_request_is_saved_and_notified_after_the_lock_is_released(self):
        self.request("!rar Metallica/Black Album (1991)")

        self.assert_not_under_the_lock("save")
        self.assert_not_under_the_lock("queue")
        self.assert_thread_started_outside_the_lock("check_queue_and_send")
        rows = config.dcc_queue["dave"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_unpacked_rar_folder"])
        self.assertFalse(dcc.queue_lock.locked(), "queue_lock leaked")

    def test_a_refused_request_leaves_the_lock_released(self):
        """The early returns inside the lock are unchanged - and still clean."""
        self.set_config(MAX_GLOBAL_QUEUE=0)

        self.request(os.path.basename(self.tree.tracks[0]))

        self.assertNotIn("sending", self.held)
        self.assertNotIn("save", self.held)
        self.assertIn(("error", ("dave", "global_full")), self.notices)
        self.assertFalse(dcc.queue_lock.locked(), "queue_lock leaked")


if __name__ == "__main__":
    unittest.main()
