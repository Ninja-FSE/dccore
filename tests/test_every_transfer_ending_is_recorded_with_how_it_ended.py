"""Every transfer that ends is recorded with how it ended (#1203).

The transfer record (#1068) kept completed transfers only, and the one failure
counter lived in memory, reset on restart and showed only in the CTCP SLOTS
line. An operator could not tell "users never accept my sends" from "my ports
are broken", nor see how many folder packs or list sends failed.

Each attempt now writes one row when it ends, with a status: completed,
failed, cancelled, or pack_failed for a folder that could not be packed. What
the bot or the user called off - the operator cancelling a pack, a stop or a
restart, a list replaced by a rebuild under a send, a receiver closing the
connection - is cancelled, so the success rate does not blame the network for
it. An old record gains the column with every row completed. The Stats page
shows, for the chosen period, the attempts, completed, failed and cancelled
per kind and a success rate; the CSV export carries the status; forgetting a
nick takes its failed and cancelled rows too.

The send paths run against fake sockets, so every one runs on every runner:
no listener is bound and nothing is dialled.
"""

import contextlib
import io
import itertools
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import support
from tests.support import DCCoreTestCase, RecordingSocket, no_disk_writes, silence_debug

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import runtime  # noqa: E402
import transfer_log  # noqa: E402
import webserver  # noqa: E402

from tests.test_dcc_resume_end_to_end import RecordingIrcSocket  # noqa: E402

REPO_ROOT = support.REPO_ROOT
USER = "SomeNick"
CHANNEL = "#somechannel"
BLOCK = 4096
CONTENT = bytes(range(256)) * 40          # 10,240 bytes: three blocks of BLOCK
DAY = 86400

COMPLETED = transfer_log.STATUS_COMPLETED
FAILED = transfer_log.STATUS_FAILED
CANCELLED = transfer_log.STATUS_CANCELLED
PACK_FAILED = transfer_log.STATUS_PACK_FAILED


def rows(columns="direction, nick, kind, name, size, bytes, status"):
    path = config.TRANSFER_LOG_FILE
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT {columns} FROM transfers ORDER BY id").fetchall()
    finally:
        conn.close()


def write_rows(many):
    """Rows straight into the file, in one transaction: (direction, kind, status, nick, age_days)."""
    transfer_log.record_unfinished(transfer_log.SENT, FAILED, "file", 1, 0)   # makes the file
    conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
    try:
        with conn:
            conn.execute("DELETE FROM transfers")
            conn.executemany(transfer_log._INSERT_WITH_STATUS, [
                (direction, nick, kind, int(time.time() - age * DAY), None, None, 10, 10,
                 None, None, None, status)
                for direction, kind, status, nick, age in many])
    finally:
        conn.close()


def set_outcomes_began(when):
    conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
    try:
        with conn:
            conn.execute("UPDATE outcomes_began SET at = ?", (int(when),))
    finally:
        conn.close()


class TheRecord(DCCoreTestCase):
    def test_an_unfinished_send_is_one_row_with_its_status_and_the_bytes_that_reached(self):
        self.assertTrue(transfer_log.record_unfinished(
            transfer_log.SENT, FAILED, "file", 5000, 1200, nick=" SomeNick ",
            item_key="Folder/Song.flac", name="Song.flac"))
        self.assertEqual(rows(), [("sent", "somenick", "file", "Song.flac", 5000, 1200, FAILED)])

    def test_a_list_and_a_download_keep_no_name(self):
        transfer_log.record_unfinished(transfer_log.SENT, CANCELLED, "list", 300, 0,
                                       item_key="k", name="SomeBot-2026-10-01.zip")
        transfer_log.record_unfinished(transfer_log.RECEIVED, FAILED, "file", 900, 40,
                                       nick="ServerOne", item_key="k", name="Track.flac")
        self.assertEqual(rows("direction, nick, kind, item_key, name, bytes, status"), [
            ("sent", None, "list", None, None, 0, CANCELLED),
            ("received", "serverone", "file", None, None, 40, FAILED)])

    def test_only_an_unfinished_status_is_taken(self):
        for status in (COMPLETED, "lost", None):
            with self.subTest(status=status):
                self.assertFalse(transfer_log.record_unfinished(transfer_log.SENT, status, "file", 1, 1))
        self.assertFalse(transfer_log.record_unfinished("sideways", FAILED, "file", 1, 1))
        self.assertEqual(rows(), [])

    def test_a_completed_row_says_so(self):
        transfer_log.record_sent("file", "k", "A.flac", 10, 10, 1.0, None, None)
        transfer_log.record_received("file", 10)
        self.assertEqual([r[-1] for r in rows()], [COMPLETED, COMPLETED])

    def test_every_figure_that_was_there_counts_the_completed_rows_alone(self):
        transfer_log.record_sent("file", "k", "A.flac", 1000, 1000, 1.0, 1000, 5.0, nick="SomeNick")
        transfer_log.record_sent("list", None, None, 50, 50, 1.0, None, None, nick="SomeNick")
        transfer_log.record_received("file", 700, nick="ServerOne")
        before = (transfer_log.summary(), transfer_log.top_files(), transfer_log.top_nicks(),
                  transfer_log.top_nicks(transfer_log.RECEIVED), transfer_log.nick_summary("SomeNick"))
        for status in (FAILED, CANCELLED, PACK_FAILED):
            transfer_log.record_unfinished(transfer_log.SENT, status, "file", 9000, 9000,
                                           nick="SomeNick", item_key="k", name="A.flac")
            transfer_log.record_unfinished(transfer_log.SENT, status, "list", 50, 50, nick="SomeNick")
            transfer_log.record_unfinished(transfer_log.RECEIVED, status, "file", 800, 800, nick="ServerOne")
        after = (transfer_log.summary(), transfer_log.top_files(), transfer_log.top_nicks(),
                 transfer_log.top_nicks(transfer_log.RECEIVED), transfer_log.nick_summary("SomeNick"))
        self.assertEqual(after, before)
        self.assertEqual(before[0]["files_sent"], 1)

    def test_the_outcomes_are_counted_per_direction_kind_and_status(self):
        write_rows([("sent", "file", COMPLETED, "a", 0), ("sent", "file", FAILED, "a", 0),
                    ("sent", "file", FAILED, "b", 0), ("sent", "album", PACK_FAILED, "a", 0),
                    ("sent", "list", CANCELLED, "a", 0), ("received", "file", FAILED, "x", 0),
                    ("sent", "file", COMPLETED, "a", 3)])
        set_outcomes_began(time.time() - 10 * DAY)
        found = transfer_log.outcomes(time.time() - DAY)
        self.assertIsNone(found["since"])
        self.assertEqual(found["rows"], {
            ("sent", "file"): {COMPLETED: 1, FAILED: 2},
            ("sent", "album"): {PACK_FAILED: 1},
            ("sent", "list"): {CANCELLED: 1},
            ("received", "file"): {FAILED: 1}})
        self.assertEqual(transfer_log.outcomes()["rows"][("sent", "file")], {COMPLETED: 2, FAILED: 2})

    def test_a_period_reaching_before_the_outcomes_began_is_counted_from_then(self):
        write_rows([("sent", "file", COMPLETED, "a", 5), ("sent", "file", FAILED, "a", 1)])
        began = int(time.time() - 2 * DAY)
        set_outcomes_began(began)
        found = transfer_log.outcomes()
        self.assertEqual(found["since"], began)
        self.assertEqual(found["rows"], {("sent", "file"): {FAILED: 1}})

    def test_forgetting_a_nick_removes_its_failed_and_cancelled_rows_too(self):
        transfer_log.record_sent("file", "k", "A.flac", 10, 10, 1.0, None, None, nick="SomeNick")
        for status in (FAILED, CANCELLED, PACK_FAILED):
            transfer_log.record_unfinished(transfer_log.SENT, status, "file", 10, 3, nick="SomeNick",
                                           item_key="k", name="A.flac")
        transfer_log.record_unfinished(transfer_log.RECEIVED, FAILED, "file", 10, 3, nick="SomeNick")
        transfer_log.record_unfinished(transfer_log.SENT, FAILED, "file", 10, 3, nick="OtherNick")

        self.assertEqual(transfer_log.forget_nick("somenick"), 5)

        self.assertEqual([(r[1], r[-1]) for r in rows()], [("othernick", FAILED)])
        with io.open(config.TRANSFER_LOG_FILE, "rb") as handle:
            self.assertNotIn(b"somenick", handle.read())

    def test_the_export_says_how_each_ended(self):
        transfer_log.record_sent("file", "k", "A.flac", 10, 10, 1.0, None, None, nick="SomeNick")
        transfer_log.record_unfinished(transfer_log.SENT, CANCELLED, "album", 10, 0, nick="SomeNick",
                                       item_key="An Album.rar", name="An Album")
        lines = "".join(webserver.record_csv_lines()).splitlines()
        self.assertEqual(lines[0].split(",")[-1], "status")
        self.assertEqual([line.split(",")[-1] for line in lines[1:]], [COMPLETED, CANCELLED])
        self.assertEqual(lines[2].split(",")[1:5], ["sent", "somenick", "album", "An Album"])


OLD_SCHEMA = """
CREATE TABLE transfers (
    id INTEGER PRIMARY KEY, direction TEXT NOT NULL, nick TEXT, kind TEXT NOT NULL,
    ended_at INTEGER NOT NULL, item_key TEXT, name TEXT, size INTEGER NOT NULL,
    bytes INTEGER NOT NULL, seconds REAL, speed INTEGER, waited REAL
);
"""


class AnOldRecord(DCCoreTestCase):
    """A file written before #1203 has no status column and no outcomes_began."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        try:
            conn.executescript(OLD_SCHEMA)
            with conn:
                conn.executemany(
                    "INSERT INTO transfers (direction, nick, kind, ended_at, item_key, name, size, bytes,"
                    " seconds, speed, waited) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [("sent", "somenick", "file", int(time.time() - DAY), "k", "A.flac", 100, 100, 1.0, 100, 2.0),
                     ("received", "serverone", "file", int(time.time() - DAY), None, None, 50, 50, None, None, None)])
        finally:
            conn.close()

    def test_its_rows_are_kept_and_read_as_completed(self):
        before = int(time.time())
        figures = transfer_log.summary()
        self.assertEqual((figures["files_sent"], figures["files_received"]), (1, 1))
        self.assertEqual([r[-1] for r in rows()], [COMPLETED, COMPLETED])
        self.assertGreaterEqual(transfer_log.outcomes_began(), before)

    def test_a_write_after_the_upgrade_goes_in_beside_them(self):
        self.assertTrue(transfer_log.record_unfinished(transfer_log.SENT, FAILED, "file", 100, 7, nick="SomeNick"))
        self.assertEqual([r[-1] for r in rows()], [COMPLETED, COMPLETED, FAILED])
        self.assertEqual(transfer_log.summary()["files_sent"], 1)

    def test_its_old_rows_do_not_count_towards_a_success_rate(self):
        """Before the upgrade a failure left no row: the old completed ones
        would make the rate read better than it was."""
        transfer_log.record_unfinished(transfer_log.SENT, FAILED, "file", 100, 7)
        found = transfer_log.outcomes()
        self.assertEqual(found["rows"], {("sent", "file"): {FAILED: 1}})
        self.assertEqual(found["since"], transfer_log.outcomes_began())

    def test_the_first_row_counts_when_the_upgrade_ends_in_the_next_second(self):
        """The row's time is taken before the open that stamps outcomes_began;
        a second ticking over in between used to leave that first failure out
        of the rate. Forced here instead of hoping for a slow disk."""
        start = int(time.time())
        clock = itertools.chain([start + 0.9], itertools.repeat(start + 1.1))
        with mock.patch.object(transfer_log.time, "time", side_effect=lambda: next(clock)):
            self.assertTrue(transfer_log.record_unfinished(transfer_log.SENT, FAILED, "file", 100, 7))
        self.assertEqual(transfer_log.outcomes()["rows"], {("sent", "file"): {FAILED: 1}})

    def test_two_connections_opening_it_do_not_trip_over_each_other(self):
        first = transfer_log._open(config.TRANSFER_LOG_FILE, 2)
        second = transfer_log._open(config.TRANSFER_LOG_FILE, 2)
        first.close()
        second.close()
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outcomes_began").fetchone()[0], 1)
        finally:
            conn.close()


class TheStatsPayload(DCCoreTestCase):
    def outcomes(self, period="all"):
        status, data = webserver.build_record_payload(period)
        self.assertEqual(status, 200)
        return {(line["direction"], line["kind"]): line for line in data["outcomes"]["rows"]}, data

    def test_the_rate_is_completed_out_of_completed_and_failed_and_a_failed_pack_is_failed(self):
        write_rows([("sent", "file", COMPLETED, "a", 0)] * 3 + [("sent", "file", FAILED, "a", 0)]
                   + [("sent", "file", CANCELLED, "a", 0)] * 2
                   + [("sent", "album", COMPLETED, "a", 0), ("sent", "album", PACK_FAILED, "a", 0)]
                   + [("received", "file", COMPLETED, "x", 0), ("received", "list", FAILED, "x", 0)])
        lines, _ = self.outcomes()
        files = lines[("sent", "file")]
        self.assertEqual((files["attempts"], files["completed"], files["failed"], files["cancelled"]), (6, 3, 1, 2))
        self.assertEqual((files["success_rate"], files["success_text"]), (75, "75%"))
        albums = lines[("sent", "album")]
        self.assertEqual((albums["attempts"], albums["failed"], albums["pack_failed"], albums["success_rate"]),
                         (2, 1, 1, 50))
        lists = lines[("sent", "list")]
        self.assertEqual((lists["attempts"], lists["success_rate"], lists["success_text"]), (0, None, ""))
        received = lines[("received", "all")]
        self.assertEqual((received["attempts"], received["completed"], received["failed"], received["success_text"]),
                         (2, 1, 1, "50%"))

    def test_only_cancelled_has_no_rate(self):
        write_rows([("sent", "list", CANCELLED, "a", 0)])
        lists = self.outcomes()[0][("sent", "list")]
        self.assertEqual((lists["attempts"], lists["cancelled"], lists["success_rate"]), (1, 1, None))

    def test_the_rate_is_rounded_down_so_100_means_nothing_failed(self):
        write_rows([("sent", "file", COMPLETED, "a", 0)] * 199 + [("sent", "file", FAILED, "a", 0)])
        self.assertEqual(self.outcomes()[0][("sent", "file")]["success_text"], "99%")

    def test_the_period_applies_and_says_when_counting_began_if_it_reaches_before(self):
        write_rows([("sent", "file", FAILED, "a", 3), ("sent", "file", COMPLETED, "a", 0)])
        began = int(time.time() - 5 * DAY)
        set_outcomes_began(began)
        lines, data = self.outcomes("day")
        self.assertEqual((lines[("sent", "file")]["attempts"], data["outcomes"]["since"]), (1, None))
        lines, data = self.outcomes("week")
        self.assertEqual((lines[("sent", "file")]["attempts"], data["outcomes"]["since"]), (2, began))


class FakeReceiver:
    """The receiving end of a send. `plan` says how it behaves:

      complete      acknowledges every byte
      hangs_up      acknowledges the first block, then closes
      closes_late   acknowledges the first block, and closes while the sender waits for
                    the last acknowledgement, after every byte has gone
      resets        takes the first block, then the connection is reset
      stalls        acknowledges the first block, then never again
    """

    def __init__(self, plan, on_block=None):
        self.plan = plan
        self.on_block = on_block
        self.sent = 0
        self.blocks = 0
        self.pending = b""
        self.closed_by_peer = False

    def settimeout(self, value):
        pass

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        if self.plan == "resets" and self.blocks:
            raise ConnectionResetError("connection reset by peer")
        self.blocks += 1
        self.sent += len(data)
        if self.on_block:
            self.on_block(self.blocks)
        if self.plan == "complete" or self.blocks == 1:
            self.pending += struct.pack("!I", self.sent)
        if self.plan == "hangs_up" and self.blocks == 2:
            self.closed_by_peer = True

    def readable(self):
        return bool(self.pending) or self.closed_by_peer

    def recv(self, size):
        if self.pending:
            data, self.pending = self.pending, b""
            return data
        if self.closed_by_peer:
            return b""
        raise socket.timeout("nothing yet")

    def close(self):
        pass


class FakeListener:
    """socket.socket for start_dcc_send(): binds, listens, and hands back the
    receiver the test made - or times out, when there is none."""

    receiver = None

    def __init__(self, *args, **kwargs):
        pass

    def setsockopt(self, *args):
        pass

    def bind(self, address):
        pass

    def settimeout(self, value):
        pass

    def listen(self, backlog):
        pass

    def accept(self):
        if FakeListener.receiver is None:
            raise socket.timeout("timed out")
        return FakeListener.receiver, ("192.0.2.10", 40000)

    def close(self):
        pass


def fake_select(read, write, error, timeout=None):
    # The send loop looks without waiting; only the wait for the final
    # acknowledgement, after the loop, waits - which is when "closes_late" closes.
    for conn in read:
        if timeout and conn.plan == "closes_late":
            conn.closed_by_peer = True
    return [conn for conn in read if conn.readable()], [], []


class SendCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = support.temp_dir(self)
        self.set_config(MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8", DCC_BLOCK_SIZE=BLOCK,
                        FILE_DIRECTORY=self.tmp, LIST_BASE_NAME="SomeBot", DCC_ACCEPT_TIMEOUT=30)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        silence_debug(announce)
        no_disk_writes(db)
        FakeListener.receiver = None
        self.addCleanup(setattr, FakeListener, "receiver", None)
        for patch in (mock.patch("socket.socket", FakeListener),
                      mock.patch.object(dcc.select, "select", fake_select),
                      mock.patch.object(dcc, "check_queue_and_send", lambda *a, **k: None),
                      mock.patch("time.sleep", lambda *a, **k: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def served(self, name="Some_Song.mp3", content=CONTENT):
        path = os.path.join(self.tmp, name)
        with io.open(path, "wb") as handle:
            handle.write(content)
        return path

    def send(self, plan=None, name="Some_Song.mp3", path=None, irc=None, on_block=None):
        path = path or self.served(name)
        if plan:
            FakeListener.receiver = FakeReceiver(plan, on_block)
        config.active_transfers[:] = [{"user": USER, "file": name, "bytes_sent": 0, "next_file_obj": name}]
        irc = irc or RecordingIrcSocket()
        self.oserve.irc_connection = irc
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.start_dcc_send(irc, USER, path, name, CHANNEL, name)
        return rows()


class EverySendEndingIsOneRow(SendCase):
    def test_a_completed_send_is_one_completed_row(self):
        self.assertEqual(self.send("complete"),
                         [("sent", "somenick", "file", "Some_Song.mp3", len(CONTENT), len(CONTENT), COMPLETED)])

    def test_an_offer_nobody_took_is_one_failed_row(self):
        self.assertEqual(self.send(None), [("sent", "somenick", "file", "Some_Song.mp3", len(CONTENT), 0, FAILED)])

    def test_an_offer_that_never_went_out_is_one_failed_row(self):
        class DeadIrc(RecordingIrcSocket):
            def sendall(self, payload):
                raise OSError("the connection to IRC is gone")
        self.assertEqual([r[-2:] for r in self.send("complete", irc=DeadIrc())], [(0, FAILED)])

    def test_a_receiver_that_hangs_up_mid_send_cancelled_it(self):
        self.assertEqual([r[-2:] for r in self.send("hangs_up")], [(BLOCK, CANCELLED)])

    def test_a_receiver_that_closes_without_acknowledging_the_end_cancelled_it(self):
        self.assertEqual([r[-2:] for r in self.send("closes_late")], [(BLOCK, CANCELLED)])

    def test_a_reset_connection_is_a_failure(self):
        self.assertEqual([r[-2:] for r in self.send("resets")], [(BLOCK, FAILED)])

    def test_a_receiver_that_stops_acknowledging_is_a_failure(self):
        self.addCleanup(setattr, dcc, "ACK_STALL_SECONDS", dcc.ACK_STALL_SECONDS)
        dcc.ACK_STALL_SECONDS = -1.0      # stalled the moment a byte is owed
        self.assertEqual([r[-2:] for r in self.send("stalls")], [(BLOCK, FAILED)])

    def test_a_list_replaced_under_its_send_was_cancelled_by_the_bot(self):
        name = "SomeBot-2026-10-01.zip"
        self.assertTrue(list_mod.is_list_artifact_name(name))
        real_getsize = os.path.getsize
        with mock.patch("os.path.getsize", lambda p: real_getsize(p) + BLOCK):
            self.assertEqual(self.send("complete", name=name),
                             [("sent", "somenick", "list", None, len(CONTENT) + BLOCK, len(CONTENT), CANCELLED)])

    def test_a_file_that_shrank_under_its_send_failed(self):
        real_getsize = os.path.getsize
        with mock.patch("os.path.getsize", lambda p: real_getsize(p) + BLOCK):
            self.assertEqual([r[-2:] for r in self.send("complete")], [(len(CONTENT), FAILED)])

    def test_a_list_gone_before_its_offer_was_replaced_and_cancelled(self):
        name = "SomeBot-2026-10-01.zip"
        missing = os.path.join(self.tmp, name)
        self.assertEqual(self.send("complete", name=name, path=missing),
                         [("sent", "somenick", "list", None, 0, 0, CANCELLED)])

    def test_a_file_gone_before_its_offer_failed(self):
        missing = os.path.join(self.tmp, "Gone.mp3")
        self.assertEqual(self.send("complete", name="Gone.mp3", path=missing),
                         [("sent", "somenick", "file", "Gone.mp3", 0, 0, FAILED)])

    def test_a_send_held_for_want_of_an_irc_connection_has_not_ended(self):
        path = self.served()
        config.active_transfers[:] = [{"user": USER, "file": "Some_Song.mp3", "bytes_sent": 0}]
        self.oserve.irc_connection = None
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.start_dcc_send(None, USER, path, "Some_Song.mp3", CHANNEL, "Some_Song.mp3")
        self.assertEqual(rows(), [])

    def test_a_stop_in_the_middle_of_a_send_leaves_one_row_and_it_is_cancelled(self):
        def stop_now(block):
            if block == 2:
                self.assertEqual(dcc.record_transfers_cut_off(), 1)
        self.assertEqual([r[-2:] for r in self.send("complete", on_block=stop_now)], [(BLOCK, CANCELLED)])

    def test_a_stop_in_the_middle_of_a_failing_send_leaves_one_row(self):
        def stop_now(block):
            if block == 1:
                dcc.record_transfers_cut_off()
        self.assertEqual([r[-1] for r in self.send("resets", on_block=stop_now)], [CANCELLED])


class AStopCutsOffWhatIsRunning(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, runtime, "pack_job", None)

    def test_each_running_send_and_the_pack_is_one_cancelled_row(self):
        self.set_config(active_transfers=[
            {"user": "SomeNick", "file": "A.flac", "bytes_sent": 7000, "resume_offset": 2000, "size": 9000,
             "transfer_outcome_identity": ("Music/A.flac", "A.flac", "file")},
            {"user": "OtherNick", "file": "B.flac", "bytes_sent": 0},     # handed a slot, never offered
        ])
        runtime.pack_job = {"user": "ThirdNick", "name": "An Album (1999)", "total": 12345,
                            "archive": "x.rar", "started": time.time(), "process": None, "cancelled": False}

        self.assertEqual(dcc.record_transfers_cut_off(), 2)

        self.assertEqual(rows("nick, kind, item_key, name, size, bytes, status"), [
            ("somenick", "file", "Music/A.flac", "A.flac", 9000, 5000, CANCELLED),
            ("thirdnick", "album", "An_Album_(1999).rar", "An_Album_(1999)", 12345, 0, CANCELLED)])
        self.assertEqual(dcc.record_transfers_cut_off(), 0, "a second stop writes nothing again")
        self.assertEqual(len(rows()), 2)

    def test_the_shutdown_writes_them(self):
        from tests.test_startup import real_oserve
        import commands
        import irc
        oserve = real_oserve()
        self.set_config(active_transfers=[
            {"user": "SomeNick", "file": "A.flac", "bytes_sent": 100, "size": 900,
             "transfer_outcome_identity": ("Music/A.flac", "A.flac", "file")}])
        with mock.patch.object(irc, "_flush_known_bots", lambda force=False: None), \
                mock.patch.object(commands, "stop_audio_reading", lambda wait=0: None), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                oserve._shut_down()
        self.assertEqual([r[-1] for r in rows()], [CANCELLED])


FAKE_RAR = r'''
import os, sys, time
target = [a for a in sys.argv if a.endswith(".rar")][0]
release = os.environ.get("FAKE_RAR_RELEASE", "")
if os.environ.get("FAKE_RAR_EXIT"):
    sys.exit(int(os.environ["FAKE_RAR_EXIT"]))
with open(target, "wb") as out:
    while not (release and os.path.exists(release)):
        out.write(b"R" * 4096)
        out.flush()
        time.sleep(0.02)
sys.exit(0)
'''


class EveryPackEndingIsOneRow(DCCoreTestCase):
    """The folder packer, with a fake rar that is a real process."""

    ALBUM = "Some Artist/Some Album (1999)"

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        CHANNEL=CHANNEL, MAX_DCC_SLOTS=3, RAR_ENABLED=True,
                        TMP_ZIP_DIR=os.path.join(self.tree.root, "tmp"),
                        bot_joined_channel=True, rar_inprogress=False)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        no_disk_writes(db)
        silence_debug(announce)
        self.oserve = support.install_fake_oserve()
        config.channel_users[CHANNEL] = {USER.lower()}
        folder = os.path.join(self.tree.music, *self.ALBUM.split("/"))
        os.makedirs(folder, exist_ok=True)
        with io.open(os.path.join(folder, "track.flac"), "wb") as handle:
            handle.write(b"\x00" * 5000)

        scratch = support.temp_dir(self)
        script = os.path.join(scratch, "fake_rar")
        with io.open(script, "w") as handle:
            handle.write(FAKE_RAR)
        self.release = os.path.join(scratch, "release")
        for name in ("FAKE_RAR_RELEASE", "FAKE_RAR_EXIT"):
            self.addCleanup(os.environ.pop, name, None)
        os.environ["FAKE_RAR_RELEASE"] = self.release
        self.rars = []
        real_popen = subprocess.Popen

        def popen(cmd, *args, **kwargs):
            process = real_popen([sys.executable, script] + list(cmd[1:]), *args, **kwargs)
            self.rars.append(process)
            return process

        for patch in (mock.patch.object(subprocess, "Popen", popen),
                      mock.patch.object(platform_compat, "rar_command", lambda configured=None: "rar-for-the-test"),
                      mock.patch.object(dcc.time, "sleep", lambda *_a: None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(setattr, runtime, "packer_thread", None)
        self.addCleanup(setattr, runtime, "pack_job", None)
        self.addCleanup(self.let_everything_finish)

    def let_everything_finish(self):
        with io.open(self.release, "w"):
            pass
        for process in self.rars:
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        self.wait_for_the_packer()

    def wait_for_the_packer(self):
        thread = runtime.packer_thread
        if thread is not None:
            thread.join(20)
            self.assertFalse(thread.is_alive(), "the packer is still running")

    def wait_for(self, condition, what, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for " + what)

    def ask(self):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.handle_download_request(RecordingSocket(), USER, "!rar " + self.ALBUM, CHANNEL)
        self.wait_for(lambda: runtime.packer_thread is not None, "the packer to start")

    def album_rows(self):
        return rows("nick, kind, item_key, name, bytes, status")

    def test_a_pack_rar_failed_is_one_pack_failed_row(self):
        os.environ["FAKE_RAR_EXIT"] = "3"
        self.ask()
        self.wait_for_the_packer()
        self.assertEqual(self.album_rows(), [
            ("somenick", "album", "Some_Album_(1999).rar", "Some_Album_(1999)", 0, PACK_FAILED)])

    def test_a_pack_with_no_rar_to_run_is_one_pack_failed_row(self):
        platform_compat.rar_command = lambda configured=None: None
        self.ask()
        self.wait_for_the_packer()
        self.assertEqual([r[-1] for r in self.album_rows()], [PACK_FAILED])

    def test_a_pack_the_operator_cancelled_is_one_cancelled_row(self):
        self.ask()
        self.wait_for(lambda: dcc.pack_status() is not None and dcc.pack_status()["done"] > 0,
                      "rar to start writing")
        self.assertIsNotNone(dcc.cancel_pack())
        self.wait_for_the_packer()
        self.assertEqual(self.album_rows(), [
            ("somenick", "album", "Some_Album_(1999).rar", "Some_Album_(1999)", 0, CANCELLED)])

    def test_a_pack_cut_off_by_a_stop_and_then_cancelled_is_one_row(self):
        self.ask()
        self.wait_for(lambda: dcc.pack_status() is not None and dcc.pack_status()["done"] > 0,
                      "rar to start writing")
        self.assertEqual(dcc.record_transfers_cut_off(), 1)
        dcc.cancel_pack()
        self.wait_for_the_packer()
        self.assertEqual([r[-1] for r in self.album_rows()], [CANCELLED])


class FakeFetchSocket:
    """The other bot's end of an active fetch: refuses the connection, or
    sends `payload` and closes."""

    payload = None

    def __init__(self, *args, **kwargs):
        self.left = FakeFetchSocket.payload

    def settimeout(self, value):
        pass

    def connect(self, address):
        if self.left is None:
            raise ConnectionRefusedError("refused")

    def recv(self, size):
        data, self.left = self.left, b""
        return data

    def sendall(self, data):
        pass

    def getpeername(self):
        return ("192.0.2.20", 50000)

    def close(self):
        pass


class EveryDownloadEndingIsOneRow(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False, CHANNEL="#chan",
                        FETCH_MAX_PER_BOT=0, FETCHED_FILES_DIR=support.temp_dir(self), MAX_FETCH_FILE_SIZE=1000)
        config.channel_users["#chan"] = {"serverone"}
        silence_debug(announce)
        patch = mock.patch("socket.socket", FakeFetchSocket)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(setattr, FakeFetchSocket, "payload", None)

    def fetch(self, payload, size=64):
        FakeFetchSocket.payload = payload
        rid = dcc_fetch.enqueue_fetch("ServerOne", "Track.flac")
        config.fetch_queue[rid].update(state="offered", offered_at=time.time())
        with contextlib.redirect_stdout(io.StringIO()):
            dcc_fetch.handle_incoming_offer(None, "ServerOne", f"DCC SEND Track.flac 3221226004 55000 {size}")
        return config.fetch_queue[rid]["state"], rows()

    def test_a_connection_refused_is_one_failed_row_without_a_name(self):
        self.assertEqual(self.fetch(None), ("failed", [("received", "serverone", "file", None, 64, 0, FAILED)]))

    def test_a_download_that_fell_short_is_one_failed_row_with_what_arrived(self):
        self.assertEqual(self.fetch(b"x" * 32), ("failed", [("received", "serverone", "file", None, 64, 32, FAILED)]))

    def test_a_finished_download_is_one_completed_row(self):
        self.assertEqual(self.fetch(b"x" * 64), ("complete", [("received", "serverone", "file", None, 64, 64, COMPLETED)]))

    def test_an_offer_refused_before_anything_was_opened_is_not_a_transfer(self):
        self.assertEqual(self.fetch(b"x" * 64, size=5000), ("failed", []))


def read(path):
    with io.open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
        return handle.read()


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(signature) {
  const start = src.indexOf(signature);
  if (start < 0) { throw new Error("missing: " + signature); }
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  return src.slice(start, i + 1);
}
function node(tag) {
  return {
    tagName: tag, children: [], className: "", title: "", hidden: false, _text: "",
    appendChild: function (child) { this.children.push(child); return child; },
    get textContent() { return this._text + this.children.map(function (c) { return c.textContent; }).join(""); },
    set textContent(v) { this._text = String(v); this.children = []; },
    set innerHTML(v) { throw new Error("innerHTML used: " + v); }
  };
}
const document = { createElement: node };
const el = { transferOutcomeRows: node("tbody"), transferOutcomeNote: node("p") };
const t = function (key) { return key === "stats.outcome.packFailed" ? "{count} could not be packed." : key; };
const draw = new Function("document", "el", "t",
  fn("function fillIn(") + "\n" + fn("function recordCell(") + "\n" +
  fn("function renderTransferOutcomes(") + "\nreturn renderTransferOutcomes;")(document, el, t);
function cells() {
  return el.transferOutcomeRows.children.map(function (row) {
    return row.children.map(function (c) { return c.textContent; });
  });
}
const lines = [
  { direction: "sent", kind: "file", attempts: 6, completed: 3, failed: 1, pack_failed: 0, cancelled: 2, success_text: "75%" },
  { direction: "sent", kind: "album", attempts: 0, completed: 0, failed: 0, pack_failed: 0, cancelled: 0, success_text: "" },
  { direction: "sent", kind: "list", attempts: 2, completed: 0, failed: 2, pack_failed: 0, cancelled: 0, success_text: "0%" },
  { direction: "received", kind: "all", attempts: 1, completed: 1, failed: 0, pack_failed: 0, cancelled: 0, success_text: "100%" }
];
const out = {};
draw({ since: null, rows: lines }, true);
out.withAlbums = cells();
out.noteHidden = el.transferOutcomeNote.hidden;
draw({ since: null, rows: lines }, false);
out.withoutAlbums = cells().map(function (r) { return r[0]; });
lines[1].attempts = 2; lines[1].failed = 2; lines[1].pack_failed = 2;
draw({ since: 86400, rows: lines }, false);
out.albumsKept = cells().map(function (r) { return r[0]; });
out.note = el.transferOutcomeNote.textContent;
out.noteShown = !el.transferOutcomeNote.hidden;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class ThePage(unittest.TestCase):
    def test_the_table_is_drawn_as_text_with_the_rate_and_the_notes(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        got = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual(got["withAlbums"], [
            ["stats.outcome.files", "6", "3", "1", "2", "75%"],
            ["stats.outcome.albums", "0", "0", "0", "0", "—"],
            ["stats.outcome.lists", "2", "0", "2", "0", "0%"],
            ["stats.outcome.received", "1", "1", "0", "0", "100%"]])
        self.assertTrue(got["noteHidden"])
        # Packing off and nothing from before: no Albums row.
        self.assertEqual(got["withoutAlbums"], ["stats.outcome.files", "stats.outcome.lists",
                                                "stats.outcome.received"])
        self.assertIn("stats.outcome.albums", got["albumsKept"])
        self.assertTrue(got["noteShown"])
        self.assertTrue(got["note"].startswith("2 could not be packed. stats.outcome.since"), got["note"])


class ThePageSource(unittest.TestCase):
    def test_the_record_draws_the_table_and_the_table_is_on_the_stats_page(self):
        js = read("web/app.js")
        render = js.split("function renderRecord(", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("renderTransferOutcomes(data.outcomes, data.albums_enabled);", render)
        stats = read("web/index.html").split('id="view-stats"', 1)[1].split("</section>", 1)[0]
        sent = stats.split('<div id="record-sent" hidden>', 1)[1].split('<div class="stat-group-label" data-i18n="stats.library">', 1)[0]
        for marker in ('<tbody id="transfer-outcome-rows"></tbody>', '<p class="stat-foot" id="transfer-outcome-note" hidden></p>'):
            with self.subTest(marker=marker):
                self.assertIn(marker, sent)

    def test_every_outcome_key_is_in_every_language(self):
        import re
        used = set(re.findall(r'"(stats\.outcome\.\w+)"', read("web/app.js") + read("web/index.html")))
        self.assertGreaterEqual(len(used), 14)
        for lang in ("en", "fr", "es"):
            keys = json.loads(read(f"web/lang/{lang}.json"))
            with self.subTest(lang=lang):
                self.assertEqual(sorted(used - set(keys)), [])


if __name__ == "__main__":
    unittest.main()
