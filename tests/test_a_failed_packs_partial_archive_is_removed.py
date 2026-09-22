"""A partial archive from a timed-out or failed rar run was never cleaned
up once the row was dropped (audit L53, #717).

subprocess.run(timeout=RAR_TIMEOUT) kills rar mid-write, and a non-zero
exit leaves whatever it wrote, at target_rar_path either way. The queue
row points at the SOURCE folder, so neither discard_orphaned_temp_archives()
nor the send's own finally ever named that file; the row was retried and
after MAX_SEND_FAILS dropped, with a multi-GB partial left in TMP_ZIP_DIR
until the same folder was packed again or an operator found it. The
auditor measured it with the real rar: a 0.4 s timeout on a 300 MB source
left a 7 MB album.rar at the exact target path.

The failure branches remove the partial now - the path is exclusively this
pack's output - and say what they removed.
"""

import contextlib
import io
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests import test_path_security as security  # noqa: E402


class AFailedRarRun(security.PoisonedQueueRowTests):
    """The real packer path, with rar stubbed to write a partial and then
    fail one way or the other."""

    def setUp(self):
        super().setUp()
        self.album = os.path.join(self.tree.music, "Some Artist", "Some Album")
        os.makedirs(self.album, exist_ok=True)
        with io.open(os.path.join(self.album, "track.flac"), "wb") as handle:
            handle.write(b"\x00" * 64)
        row = self.make_row(self.album)
        row["file"] = "Some_Album.rar"
        config.dcc_queue["dave"] = [row]
        self.set_config(TMP_ZIP_DIR=os.path.join(self.tree.root, "tmp"))
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        self.partials = []

    def rar_that_leaves_a_partial(self, outcome):
        def run(cmd, *a, **kw):
            target = [c for c in cmd if c.endswith(".rar")][0]
            with io.open(target, "wb") as handle:
                handle.write(b"RAR!" * 1024)  # a partial write
            self.partials.append(target)
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

            class Failed:
                returncode = 255
                stdout = ""
                stderr = "rar: out of disk"
            return Failed()
        dcc.subprocess.run = run

    def pack(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.check_queue_and_send(self.sock, "dave")
        return out.getvalue()

    def test_a_non_zero_exit_removes_what_it_wrote(self):
        self.rar_that_leaves_a_partial("failed")

        out = self.pack()

        self.assertEqual(len(self.partials), 1)
        self.assertFalse(os.path.exists(self.partials[0]), "the partial archive was left in TMP_ZIP_DIR")
        self.assertIn("Removed the partial archive a run that rar exited 255 left behind (4,096 bytes)", out)
        self.assertEqual(os.listdir(config.TMP_ZIP_DIR), [])

    def test_a_timeout_removes_what_it_wrote(self):
        """The auditor's own measurement, with rar's kill modelled."""
        self.rar_that_leaves_a_partial("timeout")

        out = self.pack()

        self.assertEqual(len(self.partials), 1)
        self.assertFalse(os.path.exists(self.partials[0]), "the partial archive was left in TMP_ZIP_DIR")
        self.assertIn("Removed the partial archive a run that timed out left behind", out)
        self.assertEqual(os.listdir(config.TMP_ZIP_DIR), [])

    def test_the_row_is_still_charged_and_kept_for_a_retry(self):
        """What the failure did before is untouched: the retry budget, the
        row, the interlocks."""
        self.rar_that_leaves_a_partial("failed")

        self.pack()

        rows = config.dcc_queue.get("dave", [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["send_fails"], 1)
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn("dave", config.user_processing_lock)


for _name in [n for n in dir(security.PoisonedQueueRowTests) if n.startswith("test")]:
    setattr(AFailedRarRun, _name, None)


class TheHelperOnItsOwn(unittest.TestCase):

    def test_a_missing_file_is_nothing_and_a_present_one_is_named(self):
        import tempfile
        directory = tempfile.mkdtemp(prefix="dccore-partial-")
        path = os.path.join(directory, "x.rar")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc._discard_partial_archive(path, "timed out")
        self.assertEqual(out.getvalue(), "")
        with io.open(path, "wb") as handle:
            handle.write(b"abc")
        with contextlib.redirect_stdout(out):
            dcc._discard_partial_archive(path, "timed out")

        self.assertFalse(os.path.exists(path))
        self.assertIn("(3 bytes): x.rar", out.getvalue())


if __name__ == "__main__":
    unittest.main()
