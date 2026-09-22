"""A packed album whose send failed once was deleted and had to be
re-requested and re-packed (audit M55, #657).

On any failed send of a packed .rar - the 30 s accept timeout with the
user away from the keyboard, a receiver that hung up, a stall - the finally
of start_dcc_send() deleted the archive (its step 4) before settling the
row (step 5), and release_queue_entry() then classified the row as a
"consumed temporary archive" and dropped it: "Could not send X.rar.
Removed from your queue." A plain file in the identical situation was kept
and re-offered up to MAX_SEND_FAILS times. The justification was circular -
the archive was only unusable because that same finally had just deleted
it - and a 3 GB album that took ten minutes to pack got exactly one
30-second window, holding rar_inprogress for everyone while it was packed
again.

The row is settled first now and the archive is kept for a row that was
kept; a packed row is retryable while its archive exists, and dropped -
with the archive - only when the budget runs out or the archive is gone.
"""

import io
import os
import struct
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import queue_row  # noqa: E402
from tests import test_complete_means_the_receiver_acked_it as ack  # noqa: E402

# Through the module, not imported by name: a test class imported into this
# namespace would be discovered and run a second time here.
CONTENT, RecordingIrcSocket, USER = ack.CONTENT, ack.RecordingIrcSocket, ack.USER

ARCHIVE = "Some_Album.rar"


class TheSettleRule(unittest.TestCase):
    """release_queue_entry() on its own: an archive on disk is retryable,
    one that is gone is not."""

    def setUp(self):
        from tests.support import reset_config, install_fake_oserve
        reset_config()
        self.oserve = install_fake_oserve()
        self.addCleanup(reset_config)
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp(prefix="dccore-pack-retry-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        import db
        real = db.save_dcc_queue
        db.save_dcc_queue = lambda *a, **k: None
        self.addCleanup(setattr, db, "save_dcc_queue", real)

    def packed_row(self, exists):
        path = os.path.join(self.tmp, ARCHIVE)
        if exists:
            with io.open(path, "wb") as handle:
                handle.write(b"RAR!")
        return queue_row(user=USER, filename=ARCHIVE, path=path, is_temporary_zip=True)

    def test_an_archive_still_on_disk_is_retried_like_a_plain_file(self):
        row = self.packed_row(exists=True)
        config.dcc_queue[USER] = [row]

        retained = dcc.release_queue_entry(USER, row, delivered=False, reason="socket died")

        self.assertTrue(retained)
        self.assertEqual(row["send_fails"], 1)
        self.assertIn(row, config.dcc_queue[USER])
        self.assertEqual(self.oserve.queued, [], "no 'removed from your queue' for a kept row")

    def test_an_archive_that_is_gone_is_dropped_at_once(self):
        row = self.packed_row(exists=False)
        config.dcc_queue[USER] = [row]

        retained = dcc.release_queue_entry(USER, row, delivered=False, reason="socket died")

        self.assertFalse(retained)
        self.assertNotIn(USER, config.dcc_queue)

    def test_the_budget_still_ends_it(self):
        row = self.packed_row(exists=True)
        row["send_fails"] = int(getattr(config, "MAX_SEND_FAILS", 3)) - 1
        config.dcc_queue[USER] = [row]

        retained = dcc.release_queue_entry(USER, row, delivered=False, reason="socket died")

        self.assertFalse(retained)
        self.assertNotIn(USER, config.dcc_queue)


class AFailedSendOfAPackedArchive(ack.ARealReceiver):
    """A real send over loopback of an archive in TMP_ZIP_DIR, queued as the
    packer leaves it; the receiver hangs up with most of it unacked."""

    def setUp(self):
        super().setUp()
        self.set_config(TMP_ZIP_DIR=os.path.join(self.tmp, "tmp_zips"))
        os.makedirs(config.TMP_ZIP_DIR)
        self.archive = os.path.join(config.TMP_ZIP_DIR, ARCHIVE)
        with io.open(self.archive, "wb") as handle:
            handle.write(CONTENT)
        self.row = queue_row(user=USER, filename=ARCHIVE, path=self.archive,
                             is_temporary_zip=True, channel="#somechannel")
        config.dcc_queue[USER] = [self.row]
        config.active_transfers[:] = [{"user": USER, "file": ARCHIVE, "bytes_sent": 0,
                                       "next_file_obj": ARCHIVE}]

    def start_pack_send(self):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, USER, self.archive, ARCHIVE, "#somechannel", self.row),
            daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc.handshake_seen.wait(20), "no DCC SEND handshake")
        return irc, sender

    def hang_up_early(self, irc):
        client = self.connect(irc.port())
        chunk = client.recv(4096)
        client.sendall(struct.pack("!I", len(chunk)))
        client.close()

    def test_the_archive_and_the_row_survive_the_first_failure(self):
        irc, sender = self.start_pack_send()
        self.hang_up_early(irc)
        sender.join(30)

        self.assertFalse(sender.is_alive())
        self.assertEqual(len(self.failed_lines()), 1, self.debug_lines)
        self.assertTrue(os.path.exists(self.archive), "the archive was deleted before the retry")
        self.assertIn(self.row, config.dcc_queue.get(USER, []), "the row was dropped on its first failure")
        self.assertEqual(self.row["send_fails"], 1)
        self.assertFalse(any("Removed from your queue" in m for _u, m, *_ in self.oserve.queued),
                         self.oserve.queued)

    def test_when_the_budget_runs_out_both_go(self):
        self.set_config(MAX_SEND_FAILS=1)
        irc, sender = self.start_pack_send()
        self.hang_up_early(irc)
        sender.join(30)

        self.assertFalse(sender.is_alive())
        self.assertFalse(os.path.exists(self.archive), "an archive nothing will retry is left on disk")
        self.assertNotIn(USER, config.dcc_queue)
        self.assertTrue(any("Removed from your queue" in m for _u, m, *_ in self.oserve.queued),
                        self.oserve.queued)

    def test_a_delivered_archive_is_still_cleaned_up(self):
        """The cleanup that was there is still there for the ordinary case."""
        irc, sender = self.start_pack_send()
        client = self.connect(irc.port())
        self.receive(client)
        sender.join(30)

        self.assertEqual(len(self.sent_lines()), 1, self.debug_lines)
        self.assertFalse(os.path.exists(self.archive))
        self.assertNotIn(USER, config.dcc_queue)


for _name in [n for n in dir(ack.ARealReceiver) if n.startswith("test")]:
    setattr(AFailedSendOfAPackedArchive, _name, None)


class TheOrderInTheFinally(unittest.TestCase):

    def test_the_row_is_settled_before_the_cleanup(self):
        import inspect
        body = inspect.getsource(dcc.start_dcc_send)
        settle = body.index("row_retained = release_queue_entry(")
        cleanup = body.index("file_still_needed = bool(row_retained)")

        self.assertLess(settle, cleanup)


if __name__ == "__main__":
    unittest.main()
