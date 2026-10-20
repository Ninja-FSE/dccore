"""A finished !rar archive follows the one-large-file rule (#1435).

#1387 (LARGE_FILE_MB) sends at most one large file at a time, and every path
that hands out a slot was meant to follow it. The packer's hand-off in
dcc.pack_folder_for_request() asked only whether the row was withdrawn and
whether a slot was free, so a large archive was sent the moment it was packed
while another large file was being sent. And the archive's size, stat()ed two
lines earlier, was never recorded on the row, so an archive that found no
free slot waited as a plain row that counted as small at its later dispatch.

Driven through the packer itself, with a stand-in for the rar binary that
writes an archive of the size a test asks for, and through
check_queue_and_send() for the wrapper that releases the packer interlocks
(#714) and for the sweep that sends a held-back archive later.

Every nick, channel and folder here is invented.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
from tests import support  # noqa: E402
from tests import test_path_security as security  # noqa: E402

MB = 1024 * 1024
CHANNEL = "#dccore-test"
# Somebody else's large send, already running: its real size is known.
RUNNING_BIG = {"user": "gina", "file": "Huge Archive.flac", "bytes_sent": 0, "size": 5 * MB}
RUNNING_SMALL = {"user": "hank", "file": "Little.flac", "bytes_sent": 0, "size": 4096}
LARGE_LINE = "dave's next file is large (1 MB) and another large file is being sent"


class _TimeWithoutSleep:
    """dcc's `time`, but sleep() returns at once: the packer waits 2 s for the
    disk to settle, and only dcc is given this, so no other module's sleep
    changes."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def sleep(_seconds):
        return None


class Case(security.PathSecurityBase):
    """A temp library with one album, a stand-in rar that writes an archive of
    `self.archive_bytes`, LARGE_FILE_MB = 1 and three slots."""

    def setUp(self):
        super().setUp()
        config.channel_users = {CHANNEL: {"dave", "gina", "hank"}}
        config.bot_joined_channel = True
        self.set_config(LARGE_FILE_MB=1, MAX_DCC_SLOTS=3, rar_inprogress=False)

        import platform_compat
        real_rar_command = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: os.path.join(
            self.tree.root, "rar.exe" if os.name == "nt" else "rar")
        self.addCleanup(setattr, platform_compat, "rar_command", real_rar_command)

        self.archive_bytes = MB
        self.rar_calls = []
        self.during_rar = None
        support.fake_rar_runs(self, dcc.subprocess, self._rar)

        real_time = dcc.time
        dcc.time = _TimeWithoutSleep(real_time)
        self.addCleanup(setattr, dcc, "time", real_time)

        self.album = os.path.join(self.tree.music, "Some Artist", "Some Album")
        os.makedirs(self.album, exist_ok=True)
        with io.open(os.path.join(self.album, "01.flac"), "wb") as handle:
            handle.write(b"\x00" * 1024)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)

    def _rar(self, cmd, *a, **kw):
        self.rar_calls.append(cmd)
        with io.open(cmd[-2], "wb") as handle:
            handle.truncate(self.archive_bytes)
        if self.during_rar is not None:
            self.during_rar()

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""
        return Done()

    def make_row(self):
        """dave's persisted !rar row for the album, queued."""
        row = {"file": "Some_Album.rar", "path": self.album, "channel": CHANNEL, "user_raw": "dave",
               "is_unpacked_rar_folder": True, "is_temporary_zip": True}
        config.dcc_queue["dave"] = [row]
        return row

    def running(self, *transfers):
        config.active_transfers.extend(dict(tx) for tx in transfers)

    def pack(self, row):
        """pack_folder_for_request() on `row`: (what it returned, its log)."""
        with security.quiet() as out:
            result = dcc.pack_folder_for_request(self.sock, row, lambda: "dave", CHANNEL, None)
        return result, out.getvalue()

    def queued(self, row):
        return any(entry is row for rows in config.dcc_queue.values() for entry in rows)

    def sends(self):
        """The start_dcc_send threads started: their (nick, path, offered name)."""
        return [tuple(args[1:4]) for name, args in security.InlineThread.dispatched
                if name == "start_dcc_send"]

    def sending_notices(self):
        return [n for n in self.notices if n[0] == "sending"]

    def target_archive(self):
        return os.path.normpath(os.path.join(config.TMP_ZIP_DIR, dcc._rar_archive_disk_name(self.album)))

    def users(self):
        return [tx.get("user") for tx in config.active_transfers]


class ALargeArchiveWaitsForTheLargeSend(Case):
    """A large send runs and a slot is free."""

    def test_it_is_not_claimed_and_keeps_its_place(self):
        self.running(RUNNING_BIG)
        row = self.make_row()

        result, log = self.pack(row)

        self.assertIsNone(result, "the wrapper must release the packer: nothing was handed off")
        self.assertEqual(self.users(), ["gina"], "the large archive took a slot beside a large send")
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.sending_notices(), [])
        self.assertTrue(self.queued(row), "the archive lost its place")
        self.assertEqual(config.dcc_queue["dave"], [row])
        self.assertTrue(row["path"].lower().endswith(".rar"), row["path"])
        self.assertTrue(os.path.exists(row["path"]), "the waiting archive is gone")
        self.assertEqual(row[dcc.QUEUED_SIZE_KEY], MB)
        self.assertEqual(row[dcc.QUEUED_SIZE_KEY], os.path.getsize(row["path"]))
        self.assertIn("[DCC QUEUE] " + LARGE_LINE, log)
        self.assertNotIn("[DCC-BLOCK]", log)

    def test_a_claim_with_no_size_yet_counts_as_the_large_send(self):
        """The large send is another hand-off's claim, before its send has read
        a size: its queue row's recorded size is what counts."""
        other = {"file": "Other.rar", "path": os.path.join(self.tree.root, "Other.rar"),
                 dcc.QUEUED_SIZE_KEY: 2 * MB}
        self.running({"user": "gina", "file": "Other.rar", "bytes_sent": 0, "queue_row": other})
        row = self.make_row()

        result, log = self.pack(row)

        self.assertIsNone(result)
        self.assertEqual(self.users(), ["gina"])
        self.assertEqual(self.sends(), [])
        self.assertIn(LARGE_LINE, log)


class WhenTheRuleDoesNotHoldItBack(Case):
    """The archive is claimed and sent."""

    def assert_sent(self, row, result, log):
        self.assertIs(result, True, "the packer was not handed to the send")
        self.assertEqual(self.sends(), [("dave", self.target_archive(), "Some_Album.rar")])
        self.assertEqual(len(self.sending_notices()), 1)
        (claim,) = [tx for tx in config.active_transfers if tx.get("user") == "dave"]
        self.assertIs(claim["queue_row"], row)
        self.assertNotIn(LARGE_LINE, log)
        return claim

    def test_no_large_send_running_the_large_archive_is_sent(self):
        self.running(RUNNING_SMALL)
        row = self.make_row()

        result, log = self.pack(row)

        claim = self.assert_sent(row, result, log)
        self.assertEqual(claim["queue_row"][dcc.QUEUED_SIZE_KEY], MB)
        self.assertEqual(self.users(), ["hank", "dave"])

    def test_a_small_archive_is_sent_while_a_large_send_runs(self):
        self.archive_bytes = 4096
        self.running(RUNNING_BIG)
        row = self.make_row()

        result, log = self.pack(row)

        self.assert_sent(row, result, log)
        self.assertEqual(row[dcc.QUEUED_SIZE_KEY], 4096)
        self.assertEqual(self.users(), ["gina", "dave"])

    def test_off_a_large_archive_is_sent_beside_a_large_send_and_its_size_kept(self):
        self.set_config(LARGE_FILE_MB=0)
        self.running(RUNNING_BIG)
        row = self.make_row()

        result, log = self.pack(row)

        self.assert_sent(row, result, log)
        self.assertEqual(row[dcc.QUEUED_SIZE_KEY], MB)

    def test_the_fresh_claim_is_the_large_send_before_any_size_is_read(self):
        """A dispatch right after the hand-off sees a large send
        running, through the claim's queue row."""
        row = self.make_row()

        result, log = self.pack(row)

        claim = self.assert_sent(row, result, log)
        self.assertNotIn("size", claim, "the send has not read a size yet")
        limit = dcc._large_file_bytes()
        with dcc.queue_lock:
            self.assertTrue(dcc._a_large_send_is_running(limit))
            another = {"file": "Big.flac", "path": os.path.join(self.tree.music, "Big.flac"),
                       dcc.QUEUED_SIZE_KEY: MB}
            self.assertTrue(dcc.waits_for_a_large_send([another]))


class AReusedArchiveFollowsTheRuleToo(Case):
    """Another nick's request has already packed the album, so the
    archive is reused as it is and rar never runs."""

    def setUp(self):
        super().setUp()
        target = self.target_archive()
        with io.open(target, "wb") as handle:
            handle.truncate(MB)
        # Packed under the rule dave's request follows (#1376), so he may be
        # handed it.
        config.dcc_queue["erin"] = [{"file": "Some_Album.rar", "path": target, "channel": CHANNEL,
                                     "user_raw": "erin", "is_unpacked_rar_folder": False,
                                     "is_temporary_zip": True,
                                     dcc.PACKED_WITHOUT_KEY: list(dcc.pack_leaves_out({}))}]

    def test_a_reused_large_archive_waits_while_a_large_send_runs(self):
        self.running(RUNNING_BIG)
        row = self.make_row()

        result, log = self.pack(row)

        self.assertIn("is already packed", log, "the reuse branch was not taken")
        self.assertEqual(self.rar_calls, [])
        self.assertIsNone(result)
        self.assertEqual(self.users(), ["gina"])
        self.assertEqual(self.sends(), [])
        self.assertTrue(self.queued(row))
        self.assertEqual(row["path"], self.target_archive())
        self.assertEqual(row[dcc.QUEUED_SIZE_KEY], MB)
        self.assertIn(LARGE_LINE, log)

    def test_a_reused_archive_with_no_large_send_is_sent_with_its_size(self):
        row = self.make_row()

        result, log = self.pack(row)

        self.assertIn("is already packed", log)
        self.assertEqual(self.rar_calls, [])
        self.assertIs(result, True)
        self.assertEqual(self.sends(), [("dave", self.target_archive(), "Some_Album.rar")])
        self.assertEqual(row[dcc.QUEUED_SIZE_KEY], MB)

    def test_a_reused_archive_with_no_room_records_its_size(self):
        self.set_config(MAX_DCC_SLOTS=1)
        self.running(RUNNING_SMALL)
        row = self.make_row()

        result, log = self.pack(row)

        self.assertIn("is already packed", log)
        self.assertIsNone(result)
        self.assertEqual(row[dcc.QUEUED_SIZE_KEY], MB)


class WithNoRoomTheArchiveWaitsAsALargeFile(Case):
    """Every slot is busy when the pack ends. The archive waits as
    before, and is judged by its real size when the sweep reaches it."""

    def test_no_room_records_the_size_and_the_later_sweep_holds_it_back(self):
        self.set_config(MAX_DCC_SLOTS=1)
        self.running(RUNNING_BIG)
        row = self.make_row()

        result, log = self.pack(row)

        self.assertIsNone(result)
        self.assertIn("[DCC-BLOCK] dave: all 1 slot(s) busy", log)
        self.assertNotIn(LARGE_LINE, log, "no room is the reason, not the rule")
        self.assertEqual(self.users(), ["gina"])
        self.assertEqual(self.sends(), [])
        self.assertEqual(row[dcc.QUEUED_SIZE_KEY], MB)
        self.assertTrue(dcc._row_is_large(row, dcc._large_file_bytes()))

        # A slot is added while gina's large send still runs: the sweep
        # passes the archive over, keeping its place.
        self.set_config(MAX_DCC_SLOTS=3)
        runtime.queue_waiting_since["dave"] = 1.0
        with security.quiet() as out:
            _promoted, key, _pack, _absent = dcc.claim_next_in_line()
        self.assertIsNone(key, "the waiting large archive was claimed beside a large send")
        self.assertIn(LARGE_LINE, out.getvalue())
        self.assertEqual(config.dcc_queue["dave"], [row])

        # gina's send ends: the archive is next.
        config.active_transfers[:] = []
        with security.quiet():
            _promoted, key, _pack, _absent = dcc.claim_next_in_line()
        self.assertEqual(key, "dave")


class ARowWithdrawnDuringThePack(Case):
    """The user took the row out while rar ran."""

    def test_it_is_not_sent_and_the_packer_reports_false(self):
        row = self.make_row()
        self.during_rar = lambda: config.dcc_queue.pop("dave", None)

        result, log = self.pack(row)

        self.assertEqual(len(self.rar_calls), 1)
        self.assertIs(result, False)
        self.assertIn("it is not sent", log)
        self.assertEqual(config.active_transfers, [])
        self.assertEqual(self.sends(), [])
        self.assertFalse(os.path.exists(self.target_archive()), "the unwanted archive was kept")


class ThroughTheDispatcher(Case):
    """Through check_queue_and_send(): the wrapper releases
    the packer once on the hold-back exit (#714), and the archive is sent by
    the trigger the large send's end starts - it is not stranded."""

    def test_the_held_back_archive_is_sent_when_the_large_send_ends(self):
        self.running(RUNNING_BIG)
        row = self.make_row()

        with security.quiet() as out:
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(len(self.rar_calls), 1, "the pack did not run")
        self.assertIn(LARGE_LINE, out.getvalue())
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.users(), ["gina"])
        self.assertFalse(config.rar_inprogress, "the packer interlock was left set")
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual(config.dcc_queue["dave"], [row])

        # gina's send ends, and its finally triggers the next dispatch.
        config.active_transfers[:] = []
        with security.quiet():
            dcc.check_queue_and_send(self.sock, "gina")

        self.assertEqual(self.sends(), [("dave", self.target_archive(), "Some_Album.rar")])
        self.assertEqual(self.users(), ["dave"])
        self.assertEqual(len(self.rar_calls), 1, "the archive was packed again")

    def test_the_hold_back_exit_releases_the_interlocks_once(self):
        """#714's model: the first wake claims the interlocks for 'dan', and
        he must still hold them when the packer is done. A second release
        (or none) shows here."""
        wakes = []

        def waking(irc_sock, just_finished=None):
            wakes.append((config.rar_inprogress, set(config.user_processing_lock)))
            if len(wakes) == 1:
                config.rar_inprogress = True
                config.user_processing_lock.add("dan")
        real = dcc.redispatch_waiting_pack
        dcc.redispatch_waiting_pack = waking
        self.addCleanup(setattr, dcc, "redispatch_waiting_pack", real)
        self.running(RUNNING_BIG)
        self.make_row()

        with security.quiet() as out:
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertIn(LARGE_LINE, out.getvalue(), "the hold-back exit was not the one taken")
        self.assertEqual(len(wakes), 1, wakes)
        self.assertTrue(config.rar_inprogress, "dan's claim was cleared by a second release")
        self.assertIn("dan", config.user_processing_lock)
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual(self.sends(), [])


if __name__ == "__main__":
    unittest.main()
