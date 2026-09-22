"""Two pack-body exits released the interlocks and then the wrapper's
finally released them again (audit L50, #714).

_inline_rar_packer_body()'s poisoned-row exit and no-room exit each
cleared rar_inprogress, woke the next waiting pack and dropped the user
lock, then returned None - on which inline_rar_packer()'s finally did all
three again. The second wake re-targeted the same waiting user (harmlessly
RAR-BLOCKed), but if that user's dispatch had claimed the interlocks in
the microseconds between the two releases, the wrapper's unconditional
`rar_inprogress = False` cleared the claim while their rar ran - leaving
the packer interlock open for any later trigger to start a second rar on
the same archive path, the corruption wait_for_transfers_to_finish()'s own
comment describes.

The two exits return to the wrapper without releasing; the finally is the
one place. Driven with the audit's own model: the first wake is made to
claim the interlocks for the woken user, and they must still hold them
when the packer is done.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests import test_path_security as security  # noqa: E402


class TheWokenUsersClaimSurvives(security.PoisonedQueueRowTests):
    """The audit's repro: redispatch_waiting_pack is counted, and its first
    call claims the interlocks for 'dan' the way his own dispatch would."""

    def setUp(self):
        super().setUp()
        self.wakes = []
        real = dcc.redispatch_waiting_pack

        def waking(irc_sock, just_finished=None):
            self.wakes.append((config.rar_inprogress, set(config.user_processing_lock)))
            if len(self.wakes) == 1:
                config.rar_inprogress = True
                config.user_processing_lock.add("dan")
        dcc.redispatch_waiting_pack = waking
        self.addCleanup(setattr, dcc, "redispatch_waiting_pack", real)

    def test_the_poisoned_exit_wakes_once_and_leaves_dans_claim_standing(self):
        config.dcc_queue["dave"] = [self.make_row(self.tree.secret)]

        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(len(self.wakes), 1, self.wakes)
        self.assertTrue(config.rar_inprogress, "dan's claim was cleared by a second release")
        self.assertIn("dan", config.user_processing_lock)
        self.assertNotIn("dave", config.user_processing_lock)

    def test_the_no_room_exit_does_the_same(self):
        """A pack that finishes into a full house: the archive stays queued,
        the interlocks go once."""
        import io
        album = os.path.join(self.tree.music, "Some Artist", "Some Album")
        os.makedirs(album, exist_ok=True)
        with io.open(os.path.join(album, "track.flac"), "wb") as handle:
            handle.write(b"\x00" * 64)
        row = self.make_row(album)
        row["file"] = "Some_Album.rar"
        config.dcc_queue["dave"] = [row]
        self.set_config(MAX_DCC_SLOTS=1, TMP_ZIP_DIR=os.path.join(self.tree.root, "tmp"))
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)

        class Packed:
            returncode = 0
            stdout = stderr = ""

        def rar_that_writes(cmd, *a, **kw):
            target = [c for c in cmd if c.endswith(".rar")][0]
            with io.open(target, "wb") as handle:
                handle.write(b"RAR!")
            # the house fills while rar runs
            config.active_transfers.append({"user": "someoneelse", "file": "X.flac", "bytes_sent": 0})
            return Packed()
        dcc.subprocess.run = rar_that_writes
        real_sleep = dcc.time.sleep
        dcc.time.sleep = lambda *_a: None
        self.addCleanup(setattr, dcc.time, "sleep", real_sleep)

        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(len(self.wakes), 1, self.wakes)
        self.assertTrue(config.rar_inprogress, "dan's claim was cleared by a second release")
        self.assertIn("dan", config.user_processing_lock)


for _name in [n for n in dir(security.PoisonedQueueRowTests) if n.startswith("test")]:
    setattr(TheWokenUsersClaimSurvives, _name, None)


if __name__ == "__main__":
    unittest.main()
