"""Serving files: who still needs a temp archive, and who holds a claim (#1268).

Seven findings from one audit of the send path, each driven here through the
real functions:

* A packed archive's disk name comes from its FOLDER, so every nick that asked
  for the same album names one file - and four places deleted it, each with
  its own idea of "still needed". A second nick's pack of the folder removed
  the first nick's waiting archive as "stale"; the freeze sweep and the freeze
  timer removed it without asking; the send's cleanup compared the OFFERED
  name (the leaf alone), so a different album with the same leaf name kept an
  archive on disk for good. dcc.temp_archive_in_use() is now the one answer,
  by path.
* socket.socket() failing (EMFILE) in start_dcc_send() leaked the slot, the
  nick's claim and a pack handoff's rar_inprogress.
* A /nick while rar ran left the NEW nick in user_processing_lock for good,
  and offered the archive to the old one.
* A queued file from a folder the operator stopped sharing was still sent.
* The user's own remove during their pack said "removed", and the pack
  finished and was sent anyway.

Nicks, channels and folders here are invented.
"""

import contextlib
import io
import os
import socket
import struct
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import platform_compat  # noqa: E402
import runtime  # noqa: E402

from tests import support  # noqa: E402
from tests import test_path_security as security  # noqa: E402
from tests import test_the_freeze_box_has_one_clock as clock  # noqa: E402
from tests import test_complete_means_the_receiver_acked_it as ack  # noqa: E402
from tests.support import DCCoreTestCase, RecordingSocket, queue_row  # noqa: E402

HERE = "#dccore-test"


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as buffer:
        yield buffer


def write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "wb") as handle:
        handle.write(payload)


def read(path):
    with io.open(path, "rb") as handle:
        return handle.read()


class PackCase(security.PathSecurityBase):
    """A library with one album, a TMP_ZIP_DIR, a stand-in rar, and the
    dispatcher's threads recorded rather than run (the packer runs inline)."""

    def setUp(self):
        super().setUp()
        config.bot_joined_channel = True
        config.channel_users = {HERE: {"alfa", "bravo"}}
        self.album = os.path.join(self.tree.music, "Artist", "Album")
        write(os.path.join(self.album, "track.flac"), b"\x00" * 64)
        self.set_config(TMP_ZIP_DIR=os.path.join(self.tree.root, "tmp"), MAX_DCC_SLOTS=2)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        self.archive = os.path.normpath(os.path.join(
            config.TMP_ZIP_DIR, dcc._rar_archive_disk_name(self.album)))

        real_rar_command = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: os.path.join(self.tree.root, "rar")
        self.addCleanup(setattr, platform_compat, "rar_command", real_rar_command)

        # The packer's 2-second settle, and anything else that sleeps here.
        self.sleeps = []
        real_sleep = time.sleep
        time.sleep = lambda seconds: self.sleeps.append(seconds)
        self.addCleanup(setattr, time, "sleep", real_sleep)

        # Stopping a pack terminates its process and arms a kill timer; the
        # timer is a real thread, which the recorded threads above cannot be.
        real_stop = dcc._stop_process
        dcc._stop_process = lambda process: process.terminate()
        self.addCleanup(setattr, dcc, "_stop_process", real_stop)

        self.rar_calls = []
        self.rar()

    def rar(self, returncode=0, during=None):
        """rar as the packer sees it: `during` runs while it "packs"."""
        def run(cmd, *a, **kw):
            target = [part for part in cmd if part.endswith(".rar")][0]
            self.rar_calls.append({"target": target, "existed": os.path.exists(target)})
            if during is not None:
                during()
            write(target, b"FRESH" if returncode == 0 else b"PART")

            class Done:
                stdout = ""
                stderr = "" if returncode == 0 else "rar: out of disk"
            Done.returncode = returncode
            return Done()
        support.fake_rar_runs(self, dcc.subprocess, run)

    def folder_row(self, nick):
        return {"file": "Album.rar", "path": self.album, "channel": HERE, "user_raw": nick,
                "is_unpacked_rar_folder": True, "is_temporary_zip": True}

    def packed_row(self, nick):
        return {"file": "Album.rar", "path": self.archive, "source_path": self.album,
                "channel": HERE, "user_raw": nick,
                "is_unpacked_rar_folder": False, "is_temporary_zip": True}

    def sends(self):
        """(nick, path, owns_packer) for every send the dispatcher started."""
        return [(args[1], args[2], bool(args[6]) if len(args) > 6 else False)
                for name, args in security.InlineThread.dispatched if name == "start_dcc_send"]

    def notices_to(self, nick):
        return [message for who, message, *_ in self.oserve.queued if who == nick]

    def trigger(self, nick):
        with quiet() as out:
            dcc.check_queue_and_send(self.sock, nick)
        return out.getvalue()


# --- One archive, several nicks -------------------------------------------


class ASecondRequestForAPackedFolder(PackCase):
    """dcc-3: bravo asks for the album alfa's finished archive is waiting at."""

    def setUp(self):
        super().setUp()
        write(self.archive, b"ALFA")
        self.alfa_row = self.packed_row("alfa")
        self.bravo_row = self.folder_row("bravo")
        config.dcc_queue["alfa"] = [self.alfa_row]
        config.dcc_queue["bravo"] = [self.bravo_row]
        self.rar(returncode=255)   # if rar ran at all, it would fail

    def test_the_waiting_archive_is_reused_not_packed_over(self):
        self.trigger("bravo")

        self.assertEqual(self.rar_calls, [], "rar ran over the archive alfa is waiting to be sent")
        self.assertEqual(read(self.archive), b"ALFA")
        self.assertIs(config.dcc_queue["alfa"][0], self.alfa_row)
        self.assertEqual(self.bravo_row["path"], self.archive)
        self.assertFalse(self.bravo_row["is_unpacked_rar_folder"])
        self.assertIn(("bravo", self.archive, True), self.sends())

    def test_the_name_bravo_is_offered_is_unchanged(self):
        """AutoQ matches the received name with the queued folder's own name
        (#1208): the leaf, never the disk name."""
        self.trigger("bravo")

        self.assertEqual(self.bravo_row["file"], "Album.rar")

    def test_an_archive_nobody_names_is_still_removed_before_a_fresh_pack(self):
        """The control: a stale file at the target is a crashed run's leftover,
        and `rar a` would add to it."""
        del config.dcc_queue["alfa"]
        self.rar(returncode=0)

        self.trigger("bravo")

        self.assertEqual(len(self.rar_calls), 1)
        self.assertFalse(self.rar_calls[0]["existed"], "rar was pointed at the stale archive")
        self.assertEqual(read(self.archive), b"FRESH")

    def test_an_archive_being_sent_to_alfa_is_reused_too(self):
        """alfa's row is out and its send is running; the claim carries it."""
        config.active_transfers.append({"user": "alfa", "file": "Album.rar", "bytes_sent": 0,
                                        "next_file_obj": "Album.rar", "queue_row": self.alfa_row})
        config.dcc_queue["alfa"] = []
        del config.dcc_queue["alfa"]

        self.trigger("bravo")

        self.assertEqual(self.rar_calls, [])
        self.assertEqual(read(self.archive), b"ALFA")


class TheFreezeSweepAsksFirst(PackCase):
    """dcc-2, the sweep in check_queue_and_send()."""

    def setUp(self):
        super().setUp()
        write(self.archive, b"SHARED")
        config.dcc_queue["alfa"] = [self.packed_row("alfa")]
        config.channel_users = {HERE: {"bravo"}}   # alfa has left
        config.frozen_queues["alfa"] = time.time() - dcc.FREEZE_TIMEOUT - 5
        self.set_config(MAX_DCC_SLOTS=0)   # the sweep alone, no dispatch

    def test_an_archive_bravo_still_waits_for_is_kept(self):
        config.dcc_queue["bravo"] = [self.packed_row("bravo")]

        self.trigger("system_next_trigger_fallback")

        self.assertNotIn("alfa", config.dcc_queue, "the sweep did not run")
        self.assertTrue(os.path.exists(self.archive), "the sweep deleted bravo's archive")

    def test_an_archive_only_alfa_named_goes_with_the_queue(self):
        self.trigger("system_next_trigger_fallback")

        self.assertNotIn("alfa", config.dcc_queue)
        self.assertFalse(os.path.exists(self.archive))


class TheFreezeTimerAsksFirst(clock.TheTimerThreadReadsTheSameClock):
    """dcc-2, the per-user countdown freeze_absent_user() starts."""

    def setUp(self):
        super().setUp()
        self.set_config(TMP_ZIP_DIR=os.path.join(self.make_tree().root, "tmp"))
        self.archive = os.path.join(config.TMP_ZIP_DIR, "Artist_Album.rar")
        write(self.archive, b"SHARED")
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Album.rar", path=self.archive,
                                              is_temporary_zip=True)]

    def expire(self):
        self.start_countdown()
        with dcc.queue_lock:
            config.frozen_queues["dave"] = time.time() - (dcc.FREEZE_TIMEOUT + 5)
        self.go.set()
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())
        self.assertNotIn("dave", config.dcc_queue, "the countdown did not expire")

    def test_an_archive_another_queue_names_is_kept(self):
        config.dcc_queue["bravo"] = [queue_row(user="bravo", filename="Album.rar", path=self.archive,
                                               is_temporary_zip=True)]
        self.expire()

        self.assertTrue(os.path.exists(self.archive), "the timer deleted bravo's archive")

    def test_an_archive_only_this_queue_named_goes(self):
        self.expire()

        self.assertFalse(os.path.exists(self.archive))


for _name in [n for n in dir(clock.TheTimerThreadReadsTheSameClock) if n.startswith("test")]:
    setattr(TheFreezeTimerAsksFirst, _name, None)


class AnArchiveIsKnownByItsPathNotItsLeafName(PackCase):
    """dcc-8: ArtistA/Greatest Hits and ArtistB/Greatest Hits are both
    offered as Greatest_Hits.rar, and live at two different disk names."""

    def setUp(self):
        super().setUp()
        self.album_a = os.path.join(self.tree.music, "ArtistA", "Greatest Hits")
        self.album_b = os.path.join(self.tree.music, "ArtistB", "Greatest Hits")
        self.archive_a = os.path.join(config.TMP_ZIP_DIR, dcc._rar_archive_disk_name(self.album_a))
        self.archive_b = os.path.join(config.TMP_ZIP_DIR, dcc._rar_archive_disk_name(self.album_b))
        write(self.archive_a, b"A")
        write(self.archive_b, b"B")
        self.assertNotEqual(self.archive_a, self.archive_b)

    def row(self, nick, archive, source):
        return {"file": "Greatest_Hits.rar", "path": archive, "source_path": source,
                "channel": HERE, "user_raw": nick, "is_temporary_zip": True,
                "is_unpacked_rar_folder": False}

    def test_a_send_of_the_other_album_does_not_keep_it(self):
        config.dcc_queue["alfa"] = [self.row("alfa", self.archive_a, self.album_a)]
        config.active_transfers.append({"user": "bravo", "file": "Greatest_Hits.rar", "bytes_sent": 0,
                                        "queue_row": self.row("bravo", self.archive_b, self.album_b)})
        with quiet():
            commands.handle_queue_remove(None, "alfa", HERE)

        self.assertFalse(os.path.exists(self.archive_a), "kept for a send of a different album")
        self.assertTrue(os.path.exists(self.archive_b))

    def test_a_send_of_the_same_archive_still_keeps_it(self):
        config.dcc_queue["alfa"] = [self.row("alfa", self.archive_a, self.album_a)]
        config.active_transfers.append({"user": "bravo", "file": "Greatest_Hits.rar", "bytes_sent": 0,
                                        "queue_row": self.row("bravo", self.archive_a, self.album_a)})
        with quiet():
            commands.handle_queue_remove(None, "alfa", HERE)

        self.assertTrue(os.path.exists(self.archive_a))

    def test_the_answer_is_by_path_in_every_spelling(self):
        config.dcc_queue["bravo"] = [self.row("bravo", self.archive_a, self.album_a)]
        spelled = os.path.join(os.path.dirname(self.archive_a), ".", os.path.basename(self.archive_a))

        with dcc.queue_lock:
            self.assertTrue(dcc.temp_archive_in_use(spelled))
            self.assertFalse(dcc.temp_archive_in_use(self.archive_b))
            self.assertFalse(dcc.temp_archive_in_use(spelled, ignoring=config.dcc_queue["bravo"]))


class ADeliveredArchiveIsCleanedUpByPath(ack.ARealReceiver):
    """dcc-8 end to end: start_dcc_send()'s own cleanup, over loopback."""

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(TMP_ZIP_DIR=os.path.join(tree.root, "tmp"))
        album_a = os.path.join(tree.music, "ArtistA", "Greatest Hits")
        self.album_b = os.path.join(tree.music, "ArtistB", "Greatest Hits")
        os.makedirs(self.album_b)
        self.archive = os.path.join(config.TMP_ZIP_DIR, dcc._rar_archive_disk_name(album_a))
        write(self.archive, ack.CONTENT)
        self.row = queue_row(user=ack.USER, filename="Greatest_Hits.rar", path=self.archive,
                             source_path=album_a, is_temporary_zip=True, channel="#somechannel")
        config.dcc_queue[ack.USER] = [self.row]
        config.active_transfers[:] = [{"user": ack.USER, "file": "Greatest_Hits.rar", "bytes_sent": 0,
                                       "next_file_obj": "Greatest_Hits.rar", "queue_row": self.row}]

    def deliver(self):
        irc_sock = ack.RecordingIrcSocket()
        self.oserve.irc_connection = irc_sock
        sender = threading.Thread(target=dcc.start_dcc_send,
                                  args=(irc_sock, ack.USER, self.archive, "Greatest_Hits.rar",
                                        "#somechannel", self.row), daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc_sock.handshake_seen.wait(20), "no DCC SEND handshake")
        self.receive(self.connect(irc_sock.port()))
        sender.join(30)
        self.assertFalse(sender.is_alive())
        self.assertEqual(len(self.sent_lines()), 1, self.debug_lines)

    def test_another_nicks_album_of_the_same_leaf_name_does_not_keep_it(self):
        config.dcc_queue["bravo"] = [queue_row(user="bravo", filename="Greatest_Hits.rar",
                                               path=self.album_b, is_temporary_zip=True,
                                               is_unpacked_rar_folder=True)]
        self.deliver()

        self.assertFalse(os.path.exists(self.archive), "the delivered archive stayed in TMP_ZIP_DIR")

    def test_another_nick_waiting_for_the_same_archive_keeps_it(self):
        config.dcc_queue["bravo"] = [queue_row(user="bravo", filename="Greatest_Hits.rar",
                                               path=self.archive, is_temporary_zip=True)]
        self.deliver()

        self.assertTrue(os.path.exists(self.archive))


for _name in [n for n in dir(ack.ARealReceiver) if n.startswith("test")]:
    setattr(ADeliveredArchiveIsCleanedUpByPath, _name, None)


# --- A listener that cannot be made ---------------------------------------


class ASocketThatCannotBeCreated(DCCoreTestCase):
    """dcc-1: EMFILE from socket.socket() itself."""

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.served = tree.tracks[0]
        self.row = queue_row(user="alfa", filename=os.path.basename(self.served), path=self.served)
        config.dcc_queue["alfa"] = [self.row]
        self.set_config(active_transfers=[{"user": "alfa", "file": self.row["file"], "bytes_sent": 0,
                                           "next_file_obj": self.row["file"], "queue_row": self.row}],
                        user_processing_lock={"alfa"}, rar_inprogress=True,
                        MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8")
        self.oserve.irc_connection = RecordingSocket()

        self.created = []

        def no_descriptors_left(*args, **kwargs):
            self.created.append(args)
            raise OSError(24, "Too many open files")
        real_socket = socket.socket
        socket.socket = no_descriptors_left
        self.addCleanup(setattr, socket, "socket", real_socket)

        self.threads = []
        real_thread = dcc.threading.Thread

        class Recorded:
            def __init__(inner, target=None, args=(), kwargs=None, daemon=None, **_):
                inner.target = target

            def start(inner):
                self.threads.append(getattr(inner.target, "__name__", ""))
        dcc.threading.Thread = Recorded
        self.addCleanup(setattr, dcc.threading, "Thread", real_thread)

    def send(self, owns_packer=False):
        with quiet():
            dcc.start_dcc_send(self.oserve.irc_connection, "alfa", self.served, self.row["file"],
                               HERE, self.row, owns_packer)
        self.assertEqual(len(self.created), 1, "socket.socket() was not the call that failed")

    def test_the_slot_and_the_claim_are_released(self):
        self.send()

        self.assertEqual(config.active_transfers, [], "the slot leaked")
        self.assertNotIn("alfa", config.user_processing_lock, "the nick stays claimed for good")

    def test_the_row_waits_for_a_retry_and_is_not_charged(self):
        self.send()

        self.assertIn(self.row, config.dcc_queue["alfa"])
        self.assertNotIn("send_fails", self.row)
        self.assertIn("delayed_port_retry", self.threads, "nothing would ever wake the row")

    def test_a_pack_handoff_lets_go_of_the_packer(self):
        self.send(owns_packer=True)

        self.assertFalse(config.rar_inprogress, "packing stays blocked for everyone")

    def test_a_send_that_owns_no_pack_leaves_the_packer_alone(self):
        self.send(owns_packer=False)

        self.assertTrue(config.rar_inprogress)


# --- A rename while the folder is packed ----------------------------------


class ARenameWhileRarRuns(PackCase):
    """dcc-7: alfa types /nick alfa2 while their folder is packed."""

    def setUp(self):
        super().setUp()
        config.channel_users = {HERE: {"alfa"}}
        self.row = self.folder_row("alfa")
        config.dcc_queue["alfa"] = [self.row]

    def rename(self):
        with quiet():
            irc.note_nick_change("alfa", "alfa2")

    def test_a_failed_pack_lets_go_of_the_new_nick(self):
        self.rar(returncode=255, during=self.rename)

        self.trigger("alfa")

        self.assertEqual(len(self.rar_calls), 1)
        self.assertNotIn("alfa2", config.user_processing_lock, "the renamed nick is locked out for good")
        self.assertNotIn("alfa", config.user_processing_lock)
        self.assertFalse(config.rar_inprogress)
        self.assertIsNone(runtime.pack_owner)

    def test_the_renamed_nick_is_served_again(self):
        self.rar(returncode=255, during=self.rename)
        self.trigger("alfa")

        out = self.trigger("alfa2")

        self.assertNotIn("already locked", out)
        self.assertEqual(len(self.rar_calls), 2, "the renamed nick's folder was never tried again")

    def test_the_archive_is_offered_to_the_new_nick(self):
        self.rar(returncode=0, during=self.rename)

        self.trigger("alfa")

        self.assertEqual(self.sends(), [("alfa2", self.archive, True)])
        claim = [tx for tx in config.active_transfers if tx.get("queue_row") is self.row]
        self.assertEqual([tx["user"] for tx in claim], ["alfa2"])
        self.assertIn("alfa2", config.user_processing_lock, "the send holds the claim now")
        self.assertNotIn("alfa", config.user_processing_lock)

    def test_a_rename_of_somebody_else_leaves_the_claim_alone(self):
        def rename_another():
            with quiet():
                irc.note_nick_change("bravo", "bravo2")
        self.rar(returncode=255, during=rename_another)

        self.trigger("alfa")

        self.assertNotIn("alfa", config.user_processing_lock)


# --- A folder the operator stopped sharing --------------------------------


class AFileFromAFolderNoLongerShared(PackCase):
    """dcc-9: the row was queued while the folder was shared."""

    def setUp(self):
        super().setUp()
        self.secret_file = os.path.join(self.tree.root, "Private", "taxes.pdf")
        write(self.secret_file, b"%PDF" * 10)
        self.assertFalse(dcc.path_is_in_our_library(self.secret_file))
        self.row = queue_row(user="alfa", filename="taxes.pdf", path=self.secret_file)
        config.dcc_queue["alfa"] = [self.row]
        config.channel_users = {HERE: {"alfa"}}

    def assert_refused(self):
        self.assertEqual(self.sends(), [], "a file outside every shared folder was sent")
        self.assertNotIn("alfa", config.dcc_queue, "the row is left to be tried again")
        self.assertEqual(config.active_transfers, [], "the slot stays claimed")
        self.assertNotIn("alfa", config.user_processing_lock)
        self.assertTrue(any("no longer shared" in m for m in self.notices_to("alfa")),
                        self.oserve.queued)

    def test_the_nicks_own_trigger_does_not_send_it(self):
        self.trigger("alfa")

        self.assert_refused()

    def test_the_sweep_does_not_send_it(self):
        self.trigger("system_next_trigger_fallback")

        self.assert_refused()

    def test_the_notice_names_the_file_and_never_its_path(self):
        self.trigger("alfa")

        notice = [m for m in self.notices_to("alfa") if "no longer shared" in m][0]
        self.assertIn("taxes.pdf", notice)
        self.assertNotIn("Private", notice)

    def test_a_file_inside_the_library_is_still_sent(self):
        track = os.path.join(self.album, "track.flac")
        config.dcc_queue["alfa"] = [queue_row(user="alfa", filename="track.flac", path=track)]

        self.trigger("alfa")

        self.assertEqual([path for _nick, path, _owns in self.sends()], [track])

    def test_a_packed_archive_in_the_temp_folder_is_still_sent(self):
        write(self.archive, b"RAR!")
        config.dcc_queue["alfa"] = [self.packed_row("alfa")]

        self.trigger("alfa")

        self.assertEqual([path for _nick, path, _owns in self.sends()], [self.archive])


# --- The user's own remove ------------------------------------------------


class TheUsersOwnRemoveDuringTheirPack(PackCase):
    """dcc-10: @<bot>-remove (and CTCP REMOVE) while the folder is packed."""

    def setUp(self):
        super().setUp()
        config.channel_users = {HERE: {"alfa"}}
        self.row = self.folder_row("alfa")
        config.dcc_queue["alfa"] = [self.row]

    def remove_all(self):
        with quiet():
            commands.handle_queue_remove(None, "alfa", HERE)

    def test_a_remove_while_rar_runs_stops_the_pack_and_sends_nothing(self):
        self.rar(returncode=0, during=self.remove_all)

        self.trigger("alfa")

        self.assertEqual(self.sends(), [], "the removed folder was packed and sent anyway")
        self.assertFalse(os.path.exists(self.archive), "its archive was left in TMP_ZIP_DIR")
        self.assertNotIn("alfa", config.dcc_queue)
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn("alfa", config.user_processing_lock)

    def test_the_rar_that_is_running_is_stopped(self):
        """Not left to run for up to RAR_TIMEOUT, holding the packer from
        everybody, for a folder nobody wants any more."""
        seen = {}

        def remove_and_look():
            self.remove_all()
            job = runtime.pack_job
            seen["cancelled"] = job["cancelled"]
            seen["stopped"] = job["process"].killed
        self.rar(returncode=0, during=remove_and_look)

        self.trigger("alfa")

        self.assertEqual(seen, {"cancelled": True, "stopped": True})

    def test_the_user_hears_once_that_it_is_removed_and_not_that_the_operator_cancelled(self):
        self.rar(returncode=0, during=self.remove_all)

        self.trigger("alfa")

        told = self.notices_to("alfa")
        self.assertEqual(len(told), 1, told)
        self.assertIn("completely removed", told[0])

    def test_a_remove_of_just_that_folder_stops_it_too(self):
        def remove_the_folder():
            with quiet():
                commands.handle_queue_remove_file(None, "alfa", HERE, "Album.rar")
        self.rar(returncode=0, during=remove_the_folder)

        self.trigger("alfa")

        self.assertEqual(self.sends(), [])
        self.assertFalse(os.path.exists(self.archive))

    def test_a_remove_after_rar_ended_but_before_the_send_sends_nothing(self):
        """The gap the pack job does not cover: rar is done, the row is not yet
        claimed as a send - the packer's settle sleep sits in it."""
        def settle(seconds):
            if seconds == 2.0:
                self.remove_all()
        time.sleep = settle

        self.trigger("alfa")

        self.assertEqual(len(self.rar_calls), 1)
        self.assertEqual(self.sends(), [])
        self.assertFalse(os.path.exists(self.archive))

    def test_the_operators_cancel_still_says_so(self):
        """The control: cancel_pack() without a row is the operator's, and
        the packer settles the row and tells the user as before (#1202)."""
        self.rar(returncode=0, during=lambda: dcc.cancel_pack())

        self.trigger("alfa")

        self.assertEqual(self.sends(), [])
        self.assertTrue(any("cancelled by the operator" in m for m in self.notices_to("alfa")),
                        self.oserve.queued)

    def test_a_remove_names_its_own_row_only(self):
        self.rar(returncode=0, during=lambda: self.assertIsNone(
            dcc.cancel_pack(row=self.folder_row("alfa"))))

        self.trigger("alfa")

        self.assertEqual(self.sends(), [("alfa", self.archive, True)])


class TheUsersOwnRemoveDuringASend(PackCase):
    """dcc-10, the other half: a file already being sent stays, and says so."""

    def setUp(self):
        super().setUp()
        track = os.path.join(self.album, "track.flac")
        self.sending = queue_row(user="alfa", filename="track.flac", path=track)
        self.waiting = queue_row(user="alfa", filename="next.flac", path=track + ".next")
        config.dcc_queue["alfa"] = [self.sending, self.waiting]
        config.active_transfers.append({"user": "alfa", "file": "track.flac", "bytes_sent": 0,
                                        "queue_row": self.sending})

    def test_removing_everything_keeps_the_file_being_sent(self):
        with quiet():
            commands.handle_queue_remove(None, "alfa", HERE)

        self.assertEqual(config.dcc_queue["alfa"], [self.sending])
        told = self.notices_to("alfa")
        self.assertEqual(len(told), 1, told)
        self.assertIn("track.flac", told[0])
        self.assertIn("being sent", told[0])
        self.assertNotIn("completely removed", told[0])

    def test_removing_that_file_says_it_is_being_sent(self):
        with quiet():
            commands.handle_queue_remove_file(None, "alfa", HERE, "track.flac")

        self.assertEqual(config.dcc_queue["alfa"], [self.sending, self.waiting])
        self.assertIn("being sent", self.notices_to("alfa")[0])

    def test_removing_a_waiting_file_still_removes_it(self):
        with quiet():
            commands.handle_queue_remove_file(None, "alfa", HERE, "next.flac")

        self.assertEqual(config.dcc_queue["alfa"], [self.sending])

    def test_with_nothing_being_sent_the_queue_goes_entirely(self):
        config.active_transfers[:] = []
        with quiet():
            commands.handle_queue_remove(None, "alfa", HERE)

        self.assertNotIn("alfa", config.dcc_queue)
        self.assertIn("completely removed", self.notices_to("alfa")[0])


if __name__ == "__main__":
    unittest.main()
