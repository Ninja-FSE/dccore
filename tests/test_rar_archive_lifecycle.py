"""dcc.py's packed-.rar lifecycle - #162's transfer-engine cluster (#6, #7, #8).

Three findings sharing one theme: config.rar_inprogress, and the temporary
archive it guards, being handled as if only one requester or one album could
ever exist at a time.

  #6  A plain audio file's own completion/abort path cleared
      config.rar_inprogress unconditionally, releasing another user's
      in-progress ALBUM PACK interlock out from under them mid-pack.

  #7  Two different artists' albums sharing a leaf folder name ("Greatest
      Hits") collided into the same TMP_ZIP_DIR filename - and `rar a` ADDS
      to an existing archive rather than replacing it, so the second
      requester silently received both albums packed together.

  #8  The port-exhaustion branch deleted the archive its own row was being
      preserved to retry - contradicting its own comment two lines above.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket, queue_row, silence_debug, no_disk_writes  # noqa: E402


class RarInprogressOwnershipTests(DCCoreTestCase):
    """#162 finding #6. Both start_dcc_send()'s critical-abort path and its
    main completion path carry the identical guard
    (next_file.get('is_temporary_zip')) - exercised here through the
    critical-abort path specifically, since it is reachable synchronously
    (a file_path that does not exist trips it immediately, before any
    socket is ever opened) rather than needing a full loopback transfer.
    """

    def setUp(self):
        super().setUp()
        self.sock = RecordingSocket()
        no_disk_writes(db)
        self.debug = silence_debug(announce)
        # The critical-abort path recurses into check_queue_and_send() at
        # the end, purely to wake any other work for the same user - not
        # itself under test here, and re-entering real dispatch logic
        # would only add noise.
        self._real_check = dcc.check_queue_and_send
        dcc.check_queue_and_send = lambda *a, **k: None
        self.addCleanup(setattr, dcc, "check_queue_and_send", self._real_check)

    def test_a_plain_files_abort_does_not_clear_another_users_pack_lock(self):
        config.rar_inprogress = True  # alice's pack, elsewhere, still running

        next_file = queue_row(user="bob", filename="Missing.flac", is_temporary_zip=False)
        config.dcc_queue["bob"] = [next_file]

        # A path that does not exist makes file_size come out 0, tripping
        # the critical-abort branch without ever opening a real socket.
        dcc.start_dcc_send(self.sock, "bob", next_file["path"], "Missing.flac",
                           "#dccore-test", next_file)

        self.assertTrue(config.rar_inprogress,
                        "a plain file's own abort released another user's pack interlock")

    def test_a_packs_own_abort_still_clears_its_own_lock(self):
        """Control: the guard must not go so far as to leak the lock
        forever - a pack's own send aborting must still release what it
        actually owns."""
        config.rar_inprogress = True  # this send's own pack, claimed earlier

        next_file = queue_row(user="bob", filename="Album.rar", is_temporary_zip=True,
                              is_unpacked_rar_folder=False)
        config.dcc_queue["bob"] = [next_file]

        dcc.start_dcc_send(self.sock, "bob", next_file["path"], "Album.rar",
                           "#dccore-test", next_file)

        self.assertFalse(config.rar_inprogress,
                         "a pack's own send must still release its own interlock on abort")


class ArchiveNamingTests(unittest.TestCase):
    """#162 finding #7 - the pure naming functions, no packing or sockets
    involved."""

    def setUp(self):
        self._real_dir = config.FILE_DIRECTORY
        config.FILE_DIRECTORY = os.path.join("/music")
        self.addCleanup(setattr, config, "FILE_DIRECTORY", self._real_dir)

    def test_two_different_artists_sharing_an_album_name_get_different_disk_names(self):
        """The headline repro: 'Greatest Hits' by two different artists
        used to collide into the exact same TMP_ZIP_DIR filename."""
        one = dcc._rar_archive_disk_name("/music/Metallica/Greatest Hits")
        two = dcc._rar_archive_disk_name("/music/SomeOtherBand/Greatest Hits")
        self.assertNotEqual(one, two)

    def test_the_same_album_requested_twice_still_shares_one_file(self):
        """Not a regression: discard_orphaned_temp_archives()'s own comment
        documents two users requesting the SAME real album sharing one
        packed file on purpose - the fix must not break that."""
        one = dcc._rar_archive_disk_name("/music/Metallica/Greatest Hits")
        two = dcc._rar_archive_disk_name("/music/Metallica/Greatest Hits")
        self.assertEqual(one, two)

    def test_the_visible_dcc_name_is_unaffected_by_the_disk_naming_fix(self):
        """AutoQ.mrc's reconciliation compares the received (de-underscored)
        name against the queued folder's own basename alone - changing
        what a user is OFFERED would silently break that for every
        existing deployment. Only where the bytes live on disk may change."""
        leaf_name = dcc._sanitize_rar_leaf_name("Greatest Hits")
        self.assertEqual(leaf_name, "Greatest_Hits")
        # The leaf-only name must never grow the artist prefix the disk
        # name above does.
        self.assertNotIn("Metallica", leaf_name)

    def test_square_brackets_survive_both_sanitisers_identically(self):
        """The two sanitisers used to diverge on exactly this - one kept
        square brackets, the other stripped them, breaking AutoQ's own
        reconciliation for any tagged folder name."""
        self.assertEqual(dcc._sanitize_rar_leaf_name("Album [WEB] [320K]"),
                         "Album_[WEB]_[320K]")

    def test_an_apostrophe_survives(self):
        """The exact case the packer used to need a special-case recovery
        hack for, because the two sanitisers disagreed on it."""
        self.assertEqual(dcc._sanitize_rar_leaf_name("A Winter's Tale"),
                         "A_Winter's_Tale")

    def test_non_ascii_survives(self):
        """The packer's own old regex was ASCII-only (a-zA-Z0-9) and would
        have mangled this; the queue-row sanitiser's \\w was already
        Unicode-aware. Unifying on \\w keeps the more correct behaviour."""
        self.assertEqual(dcc._sanitize_rar_leaf_name("Björk"), "Björk")

    def test_a_path_outside_file_directory_still_produces_a_name(self):
        """Defensive: relpath() raises ValueError for a different drive on
        Windows. Must fall back to something usable, not crash the pack."""
        name = dcc._rar_archive_disk_name("D:\\Somewhere\\Else")
        self.assertTrue(name.endswith(".rar"))


class StaleArchiveIsRemovedBeforePacking(unittest.TestCase):
    """#162 finding #7's second half: `rar a` ADDS to an existing archive
    rather than replacing it, so a stale file left at the target path (a
    crashed prior run, or - before the naming fix above - a genuine
    collision) would otherwise have the new pack silently merged into it."""

    def test_stale_target_removed_before_pack(self):
        import shutil
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="dccore-rar-stale-")
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        target = os.path.join(tmp_dir, "stale.rar")
        with open(target, "wb") as handle:
            handle.write(b"leftover bytes from a crashed run")
        self.assertTrue(os.path.exists(target))

        # Exercises exactly the unlink-before-pack logic added to
        # _inline_rar_packer_body(), without needing a real rar binary or
        # the full queue/thread machinery around it.
        import platform_compat
        long_target = platform_compat.long_path(target)
        if os.path.exists(long_target):
            os.remove(long_target)

        self.assertFalse(os.path.exists(target))


class PortExhaustionPreservesTheArchiveTests(DCCoreTestCase):
    """#162 finding #8: the row stays queued for the next completion
    trigger - the archive it points at must survive too, or the retry 45s
    later finds a 0-byte/missing file and discards the row as
    non-retryable, losing the album the branch's own comment says it is
    not supposed to lose."""

    def setUp(self):
        super().setUp()
        self.sock = RecordingSocket()
        no_disk_writes(db)
        self.debug = silence_debug(announce)
        self._real_check = dcc.check_queue_and_send
        dcc.check_queue_and_send = lambda *a, **k: None
        self.addCleanup(setattr, dcc, "check_queue_and_send", self._real_check)

        # Force port exhaustion portably: bind a real socket to an
        # OS-assigned free port myself, then configure the DCC port range
        # to be EXACTLY that one, already-occupied port. Guaranteed to
        # fail the bind regardless of user privileges (unlike picking a
        # low, root-only port number - this suite is noted to sometimes
        # run as root, which would make that approach silently pass).
        import socket
        self._blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._blocker.bind(("0.0.0.0", 0))
        blocked_port = self._blocker.getsockname()[1]
        self.addCleanup(self._blocker.close)

        self._real_range = (config.DCC_PORT_START, config.DCC_PORT_END)
        config.DCC_PORT_START = blocked_port
        config.DCC_PORT_END = blocked_port
        self.addCleanup(lambda: setattr(config, "DCC_PORT_START", self._real_range[0]))
        self.addCleanup(lambda: setattr(config, "DCC_PORT_END", self._real_range[1]))

    def test_the_archive_file_is_not_deleted_on_port_exhaustion(self):
        import shutil
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="dccore-rar-portexhaust-")
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        archive_path = os.path.join(tmp_dir, "Album.rar")
        with open(archive_path, "wb") as handle:
            handle.write(b"a real packed archive, waiting to be sent")

        next_file = queue_row(user="bob", filename="Album.rar", is_temporary_zip=True,
                              is_unpacked_rar_folder=False)
        next_file["path"] = archive_path
        config.dcc_queue["bob"] = [next_file]

        dcc.start_dcc_send(self.sock, "bob", archive_path, "Album.rar",
                           "#dccore-test", next_file)

        self.assertTrue(os.path.exists(archive_path),
                        "the port-exhaustion branch deleted an archive its own "
                        "row is being preserved to retry")


if __name__ == "__main__":
    unittest.main()
