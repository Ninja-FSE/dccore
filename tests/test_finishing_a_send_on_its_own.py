"""#1336: dcc.finish_send(), start_dcc_send()'s finally block, called directly.

Every exit of a send runs it, and until it was a function the only way to
reach it was a real transfer on a loopback socket (test_complete_means_the_
receiver_acked_it.py and others still drive that, for the wiring). Here it
is handed a SentFile for each kind of ending and what it did is read back.

Every nick, channel and file name here is invented.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import stats_mgr  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

USER = "bravo"
SIZE = 4_000_000


class Closes:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Ending(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        self.row = {"user": USER, "file": "Some_Album.rar", "bytes_sent": 0}
        self.set_config(active_transfers=[self.row, {"user": "charlie", "file": "other.flac"}],
                        rar_inprogress=True)
        config.user_processing_lock.add(USER)
        self.addCleanup(config.user_processing_lock.discard, USER)
        self.calls = []
        self.kept = False
        self.stub(dcc, "clear_send_offer", lambda user, port: self.calls.append(("offer", user, port)))
        self.stub(dcc, "release_queue_entry",
                  lambda user, next_file, delivered, reason="", **_: self.calls.append(
                      ("settle", user, delivered, reason)) or self.kept)
        self.stub(dcc, "_record_send_outcome",
                  lambda holder, status, identity, size, reached, nick: self.calls.append(
                      ("outcome", holder is self.row, status, reached, nick)))
        self.stub(dcc, "redispatch_waiting_pack",
                  lambda irc_sock, just_finished=None: self.calls.append(("wake pack", just_finished)))
        self.stub(dcc, "check_queue_and_send", lambda *a, **k: None)
        self.stub(announce, "send_transfer_complete",
                  lambda channel, user, file_name, *a, **k: self.calls.append(("announce", channel, user)))
        self.stub(stats_mgr, "update_speed_record",
                  lambda speed, duration=None: self.calls.append(("record", speed)))
        self.conn, self.listener = Closes(), Closes()

    def stub(self, module, name, value):
        real = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, real)

    def finish(self, completed, **fields):
        sent = dict(
            row=self.row, user=USER, next_file={"file": "Some_Album.rar", "user": USER},
            file_path=os.path.join("nowhere", "Some_Album.rar"), file_name="Some_Album.rar",
            file_size=SIZE, resume_offset=0, bytes_sent=SIZE if completed else 1_000_000,
            transfer_completed=completed, already_held=False, offered_at=100.0, start_time=100.0,
            transfer_finished_at=102.0, channel="#somechannel", irc_sock=object(),
            owns_packer=False, outcome_identity=("k", "Some_Album.rar", "file"),
            outcome_status="failed", acked=0, never_connected=False, assigned_port=5001,
            conn=self.conn, dcc_sock=self.listener)
        sent.update(fields)
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.finish_send(dcc.SentFile(**sent))
        return [call[0] for call in self.calls]


class ACompletedSend(Ending):

    def test_it_is_recorded_announced_settled_and_freed(self):
        done = self.finish(completed=True)

        self.assertEqual(done[0], "offer", "the offer is dropped first")
        self.assertIn(("record", SIZE // 2), self.calls, "two seconds for the whole file")
        self.assertIn(("announce", "#somechannel", USER), self.calls)
        self.assertIn(("settle", USER, True, "transfer complete"), self.calls)
        self.assertNotIn("outcome", done, "a completed send's outcome is record_completed_send()'s")
        self.assertEqual(config.active_transfers, [{"user": "charlie", "file": "other.flac"}])
        self.assertNotIn(USER, config.user_processing_lock)
        self.assertTrue(self.conn.closed and self.listener.closed)

    def test_a_resume_at_the_end_is_neither_a_record_nor_announced(self):
        done = self.finish(completed=True, already_held=True)

        self.assertNotIn("record", done)
        self.assertNotIn("announce", done)


class AFailedSend(Ending):

    def test_what_reached_the_receiver_is_recorded_less_what_it_held(self):
        done = self.finish(completed=False, resume_offset=100_000, acked=600_000,
                           outcome_status="cancelled")

        self.assertIn(("outcome", True, "cancelled", 500_000, USER), self.calls)
        self.assertNotIn("record", done)
        self.assertNotIn("announce", done)
        self.assertIn(("settle", USER, False, "transfer did not complete"), self.calls)

    def test_one_that_never_connected_says_so(self):
        self.finish(completed=False, never_connected=True)

        self.assertIn(("settle", USER, False, dcc.never_connected_advice()), self.calls)

    def test_an_exit_before_the_transfer_still_cleans_up(self):
        """No connection, no listener: the port-less and refused exits hand
        None for both, and the ending must not stop at closing them."""
        self.conn = self.listener = None

        self.finish(completed=False, conn=None, dcc_sock=None, transfer_finished_at=None)

        self.assertIn(("settle", USER, False, "transfer did not complete"), self.calls)
        self.assertNotIn(USER, config.user_processing_lock)


class ThePacker(Ending):

    def test_the_send_that_owns_it_releases_it_and_wakes_a_waiting_pack(self):
        self.finish(completed=True, owns_packer=True)

        self.assertFalse(config.rar_inprogress)
        self.assertIn(("wake pack", USER), self.calls)

    def test_any_other_send_leaves_it_alone(self):
        done = self.finish(completed=True, owns_packer=False)

        self.assertTrue(config.rar_inprogress)
        self.assertNotIn("wake pack", done)


class TheArchive(Ending):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-finish-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.set_config(TMP_ZIP_DIR=self.tmp)
        self.archive = os.path.join(self.tmp, "Some_Album.rar")
        with io.open(self.archive, "wb") as handle:
            handle.write(b"rar")

    def test_a_spent_archive_is_removed(self):
        self.finish(completed=True, file_path=self.archive)
        self.assertFalse(os.path.exists(self.archive))

    def test_one_whose_row_was_kept_for_a_retry_stays(self):
        self.kept = True
        self.finish(completed=False, file_path=self.archive)
        self.assertTrue(os.path.exists(self.archive))


if __name__ == "__main__":
    unittest.main()
