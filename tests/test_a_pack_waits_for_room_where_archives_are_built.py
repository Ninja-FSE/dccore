"""A !rar pack waits while there is no room for its archive (#1376).

Archives are built in TMP_ZIP_DIR, and since #1435 a finished one can wait
there - for a slot, or for a large send - while the packer moves on to the
next. Two things now hold a folder pack in the queue, keeping its place,
rather than failing it:

  * THE DISK. Before rar runs, the files to pack must fit in what is free on
    TMP_ZIP_DIR's disk with dcc_fetch.MIN_FREE_BYTES to spare - the rule
    fetching waits by. Short, that row waits, and a pack that fits goes
    ahead; a folder that could never fit, more than the whole disk, is
    refused once.
  * THE ARCHIVES ALREADY WAITING. No more than MAX_DCC_SLOTS packed archives
    wait to be sent at once (dcc.waiting_archive_cap()).

A held pack is woken again: by the queue a send's end wakes, and by the
watcher a hold starts, which looks again every PACK_HOLD_POLL_SECONDS. The
packer's interlocks are released once on the way out (#714).

Driven through dcc.pack_folder_for_request() and check_queue_and_send() with
a stand-in rar, the dispatcher's threads recorded rather than run, and the
free space a test says.

Every nick, channel and folder here is invented.
"""

import io
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
from tests import support  # noqa: E402
from tests import test_path_security as security  # noqa: E402

CHANNEL = "#dccore-test"
TRACK_BYTES = 4096
# dave's album holds this much more than erin's, all of it listed.
MORE = 1024 * 1024
MARGIN = dcc_fetch.MIN_FREE_BYTES


def write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "wb") as handle:
        handle.write(b"\x00" * size)


class _TimeWithoutSleep:
    """dcc's `time`, but sleep() returns at once."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def sleep(_seconds):
        return None


class Case(security.PathSecurityBase):
    """Two albums, dave's MORE bytes bigger than erin's, a stand-in rar, three
    slots, and `self.free` bytes free of a disk of `self.disk`."""

    def setUp(self):
        super().setUp()
        config.channel_users = {CHANNEL: {"dave", "erin", "gina", "hank"}}
        config.bot_joined_channel = True
        self.set_config(MAX_DCC_SLOTS=3, rar_inprogress=False, LARGE_FILE_MB=0,
                        LIST_IGNORED_EXTENSIONS=[".tmp"])
        import platform_compat
        real_rar_command = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: os.path.join(
            self.tree.root, "rar.exe" if os.name == "nt" else "rar")
        self.addCleanup(setattr, platform_compat, "rar_command", real_rar_command)
        self.rar_calls = []
        support.fake_rar_runs(self, dcc.subprocess, self._rar)
        real_time = dcc.time
        dcc.time = _TimeWithoutSleep(real_time)
        self.addCleanup(setattr, dcc, "time", real_time)

        self.free = 10 * MARGIN
        self.disk = 100 * MARGIN
        real_room = dcc._room_where_archives_are_built
        dcc._room_where_archives_are_built = lambda: (self.free, self.disk)
        self.addCleanup(setattr, dcc, "_room_where_archives_are_built", real_room)

        self.albums = {}
        for nick, album in (("dave", "Album One"), ("erin", "Album Two")):
            folder = os.path.join(self.tree.music, "Some Artist", album)
            write(os.path.join(folder, "01.flac"), TRACK_BYTES)
            # Left out of the list, so not packed, so not counted.
            write(os.path.join(folder, "leftover.tmp"), 5 * 1024 * 1024)
            self.albums[nick] = folder
        write(os.path.join(self.albums["dave"], "02.flac"), MORE)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        runtime.queue_waiting_since.update({"dave": 1.0, "erin": 2.0, "gina": 3.0, "hank": 4.0})

    def _rar(self, cmd, *a, **kw):
        self.rar_calls.append(cmd)
        with io.open(cmd[-2], "wb") as handle:
            handle.write(b"Rar!")

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""
        return Done()

    def pack_row(self, nick):
        row = {"file": os.path.basename(self.albums[nick]) + ".rar", "path": self.albums[nick],
               "channel": CHANNEL, "user_raw": nick, "is_unpacked_rar_folder": True,
               "is_temporary_zip": True}
        config.dcc_queue.setdefault(nick, []).append(row)
        return row

    def waiting_archive(self, nick, name=None):
        """`nick`'s finished archive, waiting to be sent."""
        path = os.path.join(config.TMP_ZIP_DIR, (name or nick) + ".rar")
        write(path, 16)
        row = {"file": (name or nick) + ".rar", "path": path, "channel": CHANNEL, "user_raw": nick,
               "is_unpacked_rar_folder": False, "is_temporary_zip": True,
               dcc.PACKED_WITHOUT_KEY: [".tmp"]}
        config.dcc_queue.setdefault(nick, []).append(row)
        return row

    def pack(self, row, owner):
        with security.quiet() as out:
            result = dcc.pack_folder_for_request(self.sock, row, lambda: owner, CHANNEL, None)
        return result, out.getvalue()

    def trigger(self, nick):
        with security.quiet() as out:
            dcc.check_queue_and_send(self.sock, nick)
        return out.getvalue()

    def dispatched(self, name):
        return [args for target, args in security.InlineThread.dispatched if target == name]

    def woken(self):
        """The nicks a dispatch was started for (a woken pack is one)."""
        return [args[1] for args in self.dispatched("check_queue_and_send")]


class ShortOfRoom(Case):

    def test_the_pack_waits_in_the_queue_and_keeps_its_place(self):
        self.free = TRACK_BYTES + MORE + MARGIN - 1
        row = self.pack_row("dave")

        result, log = self.pack(row, "dave")

        self.assertIsNone(result)
        self.assertEqual(self.rar_calls, [])
        self.assertEqual(config.dcc_queue["dave"], [row])
        self.assertTrue(row["is_unpacked_rar_folder"])
        self.assertEqual(row["path"], self.albums["dave"])
        self.assertNotIn("send_fails", row, "a pack that waits is not a failed one")
        self.assertEqual(os.listdir(config.TMP_ZIP_DIR), [])
        self.assertIsNone(runtime.pack_job)
        self.assertEqual([hold["row"] for hold in runtime.pack_space_holds], [row])
        self.assertIn("[RAR-HOLD] dave's pack of Album One waits in the queue", log)
        with dcc.queue_lock:
            self.assertIsNotNone(dcc.pack_hold_reason(row))
            self.assertFalse(dcc.packs_can_start(row))
            self.assertTrue(dcc.packs_can_start(), "one row waiting for room held every pack")

    def test_with_exactly_the_room_and_the_margin_it_packs(self):
        """What is left out of the list is not counted: the 5 MB file is not packed."""
        self.free = TRACK_BYTES + MORE + MARGIN
        row = self.pack_row("dave")

        self.pack(row, "dave")

        self.assertEqual(len(self.rar_calls), 1)
        self.assertEqual(runtime.pack_space_holds, ())

    def test_room_for_the_archive_alone_is_not_enough(self):
        self.free = (TRACK_BYTES + MORE) * 2
        row = self.pack_row("dave")

        self.pack(row, "dave")

        self.assertEqual(self.rar_calls, [])

    def test_a_disk_that_cannot_be_measured_does_not_hold_it(self):
        self.free, self.disk = None, None
        row = self.pack_row("dave")

        self.pack(row, "dave")

        self.assertEqual(len(self.rar_calls), 1)

    def test_a_folder_bigger_than_the_whole_disk_is_refused_once(self):
        self.free = 0
        self.disk = TRACK_BYTES + MORE + MARGIN - 1
        row = self.pack_row("dave")

        log = self.trigger("dave")

        self.assertEqual(self.rar_calls, [])
        self.assertIn("pack is refused: Album One needs", log)
        self.assertNotIn("dave", config.dcc_queue, "a folder that can never fit was left to wait")
        self.assertNotIn("send_fails", row)
        self.assertEqual(runtime.pack_space_holds, ())
        told = [message for who, message, *_ in self.oserve.queued if who == "dave"]
        self.assertEqual(len(told), 1, told)
        self.assertIn(dcc.PACK_FAILED_TO_USER, told[0])
        self.assertNotIn(" MB", told[0], "the user was told the disk's size")
        self.assertNotIn(os.path.abspath(config.TMP_ZIP_DIR), told[0])

    def test_one_that_fits_the_disk_waits_instead(self):
        self.free = 0
        self.disk = TRACK_BYTES + MORE + MARGIN
        self.pack_row("dave")

        self.trigger("dave")

        self.assertIn("dave", config.dcc_queue)
        self.assertEqual(len(runtime.pack_space_holds), 1)


class ShortOfRoomThroughTheDispatcher(Case):

    def setUp(self):
        super().setUp()
        # Room for erin's album, not for dave's.
        self.free = TRACK_BYTES + MARGIN
        self.row = self.pack_row("dave")

    def test_the_interlocks_are_released_and_the_watcher_started(self):
        log = self.trigger("dave")

        self.assertEqual(self.rar_calls, [])
        self.assertIn("[RAR-HOLD]", log)
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual(len(self.dispatched("_watch_held_packs")), 1)
        self.assertEqual(config.dcc_queue["dave"], [self.row])

    def test_the_interlocks_are_released_once(self):
        """#714's model: the wake claims the interlocks for 'dan', and he must
        still hold them when the packer is done."""
        wakes = []

        def waking(irc_sock, just_finished=None):
            wakes.append(just_finished)
            if len(wakes) == 1:
                config.rar_inprogress = True
                config.user_processing_lock.add("dan")
        real = dcc.redispatch_waiting_pack
        dcc.redispatch_waiting_pack = waking
        self.addCleanup(setattr, dcc, "redispatch_waiting_pack", real)

        log = self.trigger("dave")

        self.assertIn("[RAR-HOLD] dave's pack", log, "the room check was not the exit taken")
        self.assertEqual(wakes, ["dave"])
        self.assertTrue(config.rar_inprogress, "dan's claim was cleared by a second release")
        self.assertIn("dan", config.user_processing_lock)
        self.assertNotIn("dave", config.user_processing_lock)

    def test_a_pack_that_fits_goes_ahead(self):
        self.trigger("dave")
        erin = self.pack_row("erin")

        self.trigger("erin")

        self.assertEqual(len(self.rar_calls), 1, "erin's pack waited for dave's room")
        self.assertFalse(erin["is_unpacked_rar_folder"])
        self.assertTrue(self.row["is_unpacked_rar_folder"])
        self.assertEqual([hold["row"] for hold in runtime.pack_space_holds], [self.row])

    def test_the_row_waiting_for_room_is_not_woken(self):
        self.trigger("dave")
        security.InlineThread.dispatched = []

        log = self.trigger("dave")
        with security.quiet():
            dcc.check_queue_and_send(self.sock, "system_next_trigger_fallback")
            woke = dcc.redispatch_waiting_pack(self.sock)

        self.assertEqual(self.rar_calls, [])
        self.assertIn("[RAR-HOLD] dave's folder pack waits in the queue", log)
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertNotIn("dave", self.woken(), "the row waiting for room was woken")
        self.assertIsNone(woke)

    def watch(self, watch_args, looks_allowed):
        real_poll = dcc.PACK_HOLD_POLL_SECONDS
        dcc.PACK_HOLD_POLL_SECONDS = 0
        self.addCleanup(setattr, dcc, "PACK_HOLD_POLL_SECONDS", real_poll)
        # A watcher that never stops watching would loop here for ever: its
        # sleep gives up after a few looks instead.
        looks = []

        class Enough(BaseException):
            """Not an Exception: the watcher's own guard is for those."""

        def a_few_looks(seconds):
            looks.append(seconds)
            if len(looks) > looks_allowed:
                raise Enough()
        dcc.time.sleep = a_few_looks
        with security.quiet() as out:
            try:
                dcc._watch_held_packs(*watch_args)
            except Enough:
                pass
            finally:
                del dcc.time.sleep   # back to the sleep that returns at once
        return looks, out.getvalue()

    def test_once_there_is_room_the_watcher_wakes_it_and_it_is_packed(self):
        self.trigger("dave")
        (watch_args,) = self.dispatched("_watch_held_packs")
        security.InlineThread.dispatched = []
        self.free = 10 * MARGIN

        looks, out = self.watch(watch_args, 3)

        self.assertEqual(looks, [0], "the watcher did not stop once there was room")
        self.assertIn("[RAR-WAKE]", out)
        self.assertEqual(runtime.pack_space_holds, ())
        self.assertIsNone(runtime.pack_hold_watch)
        self.assertEqual(self.woken(), ["dave"])
        self.trigger("dave")
        self.assertEqual(len(self.rar_calls), 1)
        self.assertFalse(self.row["is_unpacked_rar_folder"])

    def test_one_that_stops_waiting_is_woken_while_another_still_waits(self):
        self.free = 0
        self.trigger("dave")
        erin = self.pack_row("erin")
        self.trigger("erin")
        self.assertEqual(len(runtime.pack_space_holds), 2)
        (watch_args,) = self.dispatched("_watch_held_packs")
        security.InlineThread.dispatched = []
        self.free = TRACK_BYTES + MARGIN

        looks, out = self.watch(watch_args, 1)

        self.assertEqual(looks, [0, 0], "the watcher stopped with dave still waiting")
        self.assertEqual([hold["row"] for hold in runtime.pack_space_holds], [self.row])
        self.assertEqual(self.woken(), ["erin"])
        self.assertIsNone(runtime.pack_hold_watch, "the watcher's mark outlived it")
        self.trigger("erin")
        self.assertFalse(erin["is_unpacked_rar_folder"])

    def test_a_look_that_fails_still_wakes_the_queue(self):
        self.trigger("dave")
        (watch_args,) = self.dispatched("_watch_held_packs")
        security.InlineThread.dispatched = []
        real_look = dcc._look_at_held_packs

        def broken():
            raise RuntimeError("a look that failed")
        dcc._look_at_held_packs = broken
        self.addCleanup(setattr, dcc, "_look_at_held_packs", real_look)

        looks, out = self.watch(watch_args, 3)

        self.assertEqual(looks, [0])
        self.assertIn("Looking at the held folder packs failed: a look that failed", out)
        self.assertIn("[RAR-WAKE]", out)
        self.assertIsNone(runtime.pack_hold_watch)

    def test_the_watcher_keeps_watching_while_the_disk_is_short(self):
        self.trigger("dave")

        with security.quiet():
            self.assertEqual(dcc._look_at_held_packs(), (True, False))
            # dave takes his row out: nothing waits for him any more.
            config.dcc_queue.pop("dave")
            self.assertEqual(dcc._look_at_held_packs(), (False, True))
        self.assertEqual(runtime.pack_space_holds, ())

    def test_one_watcher_at_a_time(self):
        self.free = 0
        self.trigger("dave")
        self.pack_row("erin")
        self.trigger("erin")

        self.assertEqual(len(runtime.pack_space_holds), 2)
        self.assertEqual(len(self.dispatched("_watch_held_packs")), 1)

    def test_a_watcher_that_cannot_start_is_not_marked_as_running(self):
        def no_threads(*a, **kw):
            raise RuntimeError("can't start new thread")
        recorded = dcc.threading.Thread
        dcc.threading.Thread = lambda *a, **kw: types.SimpleNamespace(start=no_threads)
        self.addCleanup(setattr, dcc.threading, "Thread", recorded)

        with security.quiet():
            self.assertFalse(dcc._arm_pack_hold_watch(self.sock))
        self.assertIsNone(runtime.pack_hold_watch)


class TooManyArchivesWait(Case):

    def test_counted_by_path_and_not_while_being_sent(self):
        one = self.waiting_archive("gina", "Shared")
        self.waiting_archive("hank", "Shared")
        with dcc.queue_lock:
            self.assertEqual(dcc.waiting_archive_count(), 1)
        config.active_transfers.append({"user": "gina", "file": "Shared.rar", "bytes_sent": 0,
                                        "queue_row": one})
        with dcc.queue_lock:
            self.assertEqual(dcc.waiting_archive_count(), 0)

    def test_the_cap_is_max_dcc_slots_and_at_least_one(self):
        for slots, cap in ((3, 3), (1, 1), (0, 1)):
            with self.subTest(slots=slots):
                self.set_config(MAX_DCC_SLOTS=slots)
                self.assertEqual(dcc.waiting_archive_cap(), cap)

    def test_an_archive_behind_its_own_nicks_pack_does_not_wait(self):
        """One slot, and gina's queue is a pack with her own archive behind
        it: counted, the archive would hold the pack it waits behind for ever."""
        self.set_config(MAX_DCC_SLOTS=1)
        pack = {"file": "Album One.rar", "path": self.albums["dave"], "channel": CHANNEL, "user_raw": "gina",
                "is_unpacked_rar_folder": True, "is_temporary_zip": True}
        config.dcc_queue["gina"] = [pack]
        self.waiting_archive("gina")
        with dcc.queue_lock:
            self.assertEqual(dcc.waiting_archive_count(), 0)
            self.assertIsNone(dcc.pack_hold_reason(pack))
            self.assertTrue(dcc.packs_can_start(pack))

        self.trigger("gina")

        self.assertEqual(len(self.rar_calls), 1, "gina's pack was held by the archive behind it")

    def test_one_moved_behind_a_pack_stops_counting(self):
        import commands
        self.set_config(MAX_DCC_SLOTS=1)
        self.waiting_archive("gina")
        pack = {"file": "Album One.rar", "path": self.albums["dave"], "channel": CHANNEL, "user_raw": "gina",
                "is_unpacked_rar_folder": True, "is_temporary_zip": True}
        config.dcc_queue["gina"].append(pack)
        with dcc.queue_lock:
            self.assertEqual(dcc.waiting_archive_count(), 1)

        ok, said = commands.move_queued_file("gina", 2, "up")

        self.assertTrue(ok, said)
        self.assertIs(config.dcc_queue["gina"][0], pack)
        with dcc.queue_lock:
            self.assertEqual(dcc.waiting_archive_count(), 0)
            self.assertTrue(dcc.packs_can_start(pack))

    def test_an_archive_behind_a_plain_file_still_waits(self):
        self.set_config(MAX_DCC_SLOTS=1)
        config.dcc_queue["gina"] = [{"file": "Song.mp3", "path": os.path.join(self.tree.music, "Song.mp3"),
                                     "channel": CHANNEL, "user_raw": "gina", "is_temporary_zip": False}]
        self.waiting_archive("gina")
        with dcc.queue_lock:
            self.assertEqual(dcc.waiting_archive_count(), 1)

    def test_held_at_the_cap_and_not_below_it(self):
        self.set_config(MAX_DCC_SLOTS=2)
        self.waiting_archive("gina")
        with dcc.queue_lock:
            self.assertIsNone(dcc.pack_hold_reason(), "one archive below the cap held the pack")
        self.waiting_archive("hank")
        with dcc.queue_lock:
            self.assertIn("2 packed archive(s) are already waiting", dcc.pack_hold_reason())

    def test_a_pack_at_the_cap_waits_and_is_woken_when_one_is_sent(self):
        self.set_config(MAX_DCC_SLOTS=2)
        gina = self.waiting_archive("gina")
        self.waiting_archive("hank")
        row = self.pack_row("dave")

        log = self.trigger("dave")

        self.assertEqual(self.rar_calls, [])
        self.assertIn("[RAR-HOLD] dave's folder pack waits in the queue: 2 packed archive(s)", log)
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual(config.dcc_queue["dave"], [row])
        self.assertEqual(len(self.dispatched("_watch_held_packs")), 1)
        self.assertNotIn("dave", self.woken())

        # gina's archive is sent: her claim and her row go, as a send's end
        # takes them, and its trigger runs the queue.
        security.InlineThread.dispatched = []
        config.active_transfers[:] = [tx for tx in config.active_transfers if tx.get("queue_row") is not gina]
        config.dcc_queue.pop("gina", None)
        self.trigger("gina")

        self.assertIn("dave", self.woken())
        self.trigger("dave")
        self.assertEqual(len(self.rar_calls), 1)

    def test_an_archive_the_packer_leaves_waiting_counts(self):
        """One slot: dave's finished archive finds it busy and waits, which
        is the cap, so erin's pack waits for it - and the packer's release
        starts the watcher rather than waking her."""
        self.set_config(MAX_DCC_SLOTS=1)
        dave = self.pack_row("dave")
        erin = self.pack_row("erin")
        # A send that holds the one slot, started after the pack was claimed.
        self.running = {"user": "hank", "file": "Little.flac", "bytes_sent": 0, "size": 10}

        real_rar = self._rar

        def busy_meanwhile(cmd, *a, **kw):
            config.active_transfers.append(dict(self.running))
            return real_rar(cmd, *a, **kw)
        support.fake_rar_runs(self, dcc.subprocess, busy_meanwhile)

        log = self.trigger("dave")

        self.assertEqual(len(self.rar_calls), 1)
        self.assertIn("[DCC-BLOCK] dave", log)
        self.assertFalse(dave["is_unpacked_rar_folder"], "dave's archive was not made")
        with dcc.queue_lock:
            self.assertEqual(dcc.waiting_archive_count(), 1)
            self.assertFalse(dcc.packs_can_start())
        self.assertNotIn("erin", self.woken())
        self.assertEqual(len(self.dispatched("_watch_held_packs")), 1)
        self.assertTrue(erin["is_unpacked_rar_folder"])


if __name__ == "__main__":
    unittest.main()
