"""A record of finished transfers, with the nick each one went to or came from (#1068).

One row is written when a transfer ends, so the figures an operator wants -
the most-sent files, files and lists sent, top and average speed, files
received and how big, the average wait in the queue, and the nicks with the
most files - can be worked out from one place. The nick is kept in lower case;
no host and no channel is.
"""

import contextlib
import io
import os
import shutil
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import announce  # noqa: E402
import dcc  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import transfer_log  # noqa: E402

from tests import dcc_ports  # noqa: E402
from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_a_failing_bot_is_paused import TcpLike  # noqa: E402
from tests.test_dcc_resume_end_to_end import RecordingIrcSocket, loopback_is_usable  # noqa: E402

USER = "SomeNick"
CHANNEL = "#somechannel"
CONTENT = bytes(range(256)) * 400          # 102,400 bytes


class Case(DCCoreTestCase):
    def sent(self, name="Song.mp3", key=None, kind="file", size=1000, speed=None, waited=None,
             seconds=1.0, wire=None, nick=None):
        return transfer_log.record_sent(kind, key or name, name, size,
                                        size if wire is None else wire, seconds, speed, waited, nick=nick)

    def rows(self):
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        try:
            return conn.execute("SELECT * FROM transfers ORDER BY id").fetchall()
        finally:
            conn.close()


class WhatIsKept(Case):
    def test_a_row_has_a_nick_and_no_host_or_channel_column(self):
        self.sent()
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(transfers)")}
        conn.close()
        self.assertEqual(columns, {"id", "direction", "nick", "kind", "ended_at", "item_key", "name",
                                   "size", "bytes", "seconds", "speed", "waited"})

    def test_a_list_is_kept_without_its_name(self):
        self.sent("Bot-List-2026-10-01.zip", kind="list")
        row = self.rows()[0]
        self.assertEqual((row[3], row[5], row[6]), ("list", None, None))

    def test_a_received_file_is_kept_without_its_name(self):
        transfer_log.record_received("file", 4096)
        row = self.rows()[0]
        self.assertEqual((row[1], row[3], row[5], row[6], row[7]), ("received", "file", None, None, 4096))

    def test_the_nick_is_kept_in_lower_case(self):
        self.sent(nick="  SomeNick ")
        transfer_log.record_received("file", 10, nick="OtherBot")
        self.assertEqual([row[2] for row in self.rows()], ["somenick", "otherbot"])

    def test_a_row_without_a_nick_keeps_none(self):
        self.sent()
        self.sent(nick="   ")
        self.assertEqual([row[2] for row in self.rows()], [None, None])

    def test_the_record_can_be_turned_off(self):
        self.set_config(TRANSFER_LOG_FILE="")
        self.assertFalse(self.sent())
        self.assertEqual(transfer_log.summary()["files_sent"], 0)

    def test_a_write_that_fails_is_dropped_and_not_raised(self):
        self.set_config(TRANSFER_LOG_FILE=tempfile.gettempdir())   # a directory, not a file
        self.assertFalse(self.sent())


class TheFigures(Case):
    def test_an_empty_record_says_zero_and_no_wait(self):
        self.assertEqual(transfer_log.summary(), {
            "files_sent": 0, "lists_sent": 0, "bytes_sent": 0, "top_speed": 0, "average_speed": 0,
            "queue_wait_seconds": None, "files_received": 0, "bytes_received": 0})
        self.assertEqual(transfer_log.top_files(), [])

    def test_files_and_lists_are_counted_apart(self):
        self.sent("A.mp3")
        self.sent("B.mp3")
        self.sent("Pack.rar", kind="album")
        self.sent("List-2026-10-01.zip", kind="list")
        figures = transfer_log.summary()
        self.assertEqual((figures["files_sent"], figures["lists_sent"]), (3, 1))

    def test_the_bytes_sent_leave_the_lists_out(self):
        self.sent("A.mp3", size=1000)
        self.sent("List.zip", kind="list", size=500000)
        self.assertEqual(transfer_log.summary()["bytes_sent"], 1000)

    def test_top_speed_and_a_weighted_average(self):
        self.sent("A.mp3", size=1000, seconds=1.0, speed=1000)
        self.sent("B.mp3", size=9000, seconds=1.0, speed=9000)
        self.sent("Tiny.mp3", size=10, seconds=0.01, speed=None)
        figures = transfer_log.summary()
        self.assertEqual(figures["top_speed"], 9000)
        self.assertEqual(figures["average_speed"], 5000)

    def test_the_wait_is_averaged_over_the_rows_that_know_it(self):
        self.sent("A.mp3", waited=10)
        self.sent("B.mp3", waited=30)
        self.sent("C.mp3", waited=None)
        self.assertEqual(transfer_log.summary()["queue_wait_seconds"], 20)

    def test_received_files_and_their_size_leave_the_lists_out(self):
        transfer_log.record_received("file", 1000)
        transfer_log.record_received("album", 5000)
        transfer_log.record_received("list", 70000)
        figures = transfer_log.summary()
        self.assertEqual((figures["files_received"], figures["bytes_received"]), (2, 6000))

    def test_what_was_sent_does_not_count_as_received(self):
        self.sent("A.mp3")
        figures = transfer_log.summary()
        self.assertEqual((figures["files_received"], figures["bytes_received"]), (0, 0))

    def test_a_period_leaves_out_what_came_before_it(self):
        self.sent("Old.mp3")
        time.sleep(1.1)
        cut = int(time.time())
        self.sent("New.mp3")
        self.assertEqual(transfer_log.summary(since=cut)["files_sent"], 1)
        self.assertEqual(transfer_log.top_files(since=cut), [("New.mp3", 1)])


class TheNicks(Case):
    def test_the_nick_with_most_files_comes_first_then_the_larger_total_then_the_name(self):
        for _ in range(3):
            self.sent("A.mp3", size=100, nick="nickb")
        self.sent("B.mp3", size=500, nick="nicka")
        self.sent("C.mp3", size=500, nick="nicka")
        self.sent("D.mp3", size=900, nick="nickc")
        self.sent("E.mp3", size=900, nick="nickd")
        self.sent("F.mp3", size=900, nick="nickd")
        self.assertEqual(transfer_log.top_nicks(),
                         [("nickb", 3, 300), ("nickd", 2, 1800), ("nicka", 2, 1000), ("nickc", 1, 900)])

    def test_lists_and_nickless_rows_are_not_ranked(self):
        self.sent("Bot-List.zip", kind="list", nick="nicka")
        self.sent("A.mp3", nick=None)
        self.sent("B.mp3", nick="nickb")
        self.assertEqual(transfer_log.top_nicks(), [("nickb", 1, 1000)])

    def test_sent_and_received_are_ranked_apart(self):
        self.sent("A.mp3", nick="nicka")
        transfer_log.record_received("file", 700, nick="botone")
        transfer_log.record_received("album", 300, nick="botone")
        self.assertEqual(transfer_log.top_nicks(transfer_log.SENT), [("nicka", 1, 1000)])
        self.assertEqual(transfer_log.top_nicks(transfer_log.RECEIVED), [("botone", 2, 1000)])

    def test_the_limit_and_the_period_apply(self):
        for n in range(5):
            self.sent(f"{n}.mp3", nick=f"nick{n}")
        self.assertEqual(len(transfer_log.top_nicks(limit=3)), 3)
        self.assertEqual(transfer_log.top_nicks(since=time.time() + 60), [])

    def test_one_nick_in_figures(self):
        self.sent("A.mp3", size=100, nick="NickA")
        self.sent("B.mp3", size=200, nick="nicka")
        self.sent("Bot-List.zip", kind="list", nick="nicka")
        self.sent("C.mp3", size=900, nick="nickb")
        transfer_log.record_received("file", 50, nick="NICKA")
        self.assertEqual(transfer_log.nick_summary("NickA"), {
            "files_sent": 2, "lists_sent": 1, "bytes_sent": 300, "files_received": 1, "bytes_received": 50})

    def test_an_unknown_or_empty_nick_has_all_zeros(self):
        zero = {"files_sent": 0, "lists_sent": 0, "bytes_sent": 0, "files_received": 0, "bytes_received": 0}
        self.assertEqual(transfer_log.nick_summary("nobody"), zero)
        self.assertEqual(transfer_log.nick_summary(""), zero)
        self.assertEqual(transfer_log.nick_summary(None), zero)

    def test_forgetting_a_nick_removes_its_rows_and_only_its_rows(self):
        self.sent("A.mp3", nick="nicka")
        self.sent("B.mp3", nick="nicka")
        self.sent("C.mp3", nick="nickb")
        self.assertEqual(transfer_log.forget_nick("NickA"), 2)
        self.assertEqual(transfer_log.top_nicks(), [("nickb", 1, 1000)])
        self.assertEqual(transfer_log.summary()["files_sent"], 1)

    def test_forgetting_nobody_removes_nothing(self):
        self.sent("A.mp3", nick="nicka")
        self.assertEqual(transfer_log.forget_nick(""), 0)
        self.assertEqual(transfer_log.forget_nick(None), 0)
        self.assertEqual(transfer_log.forget_nick("nobody"), 0)
        self.assertEqual(transfer_log.summary()["files_sent"], 1)

    def test_forgetting_everything_empties_the_record(self):
        self.sent("A.mp3", nick="nicka")
        transfer_log.record_received("file", 10, nick="botone")
        self.assertEqual(transfer_log.forget_all(), 2)
        self.assertEqual(transfer_log.summary()["files_sent"], 0)
        self.assertEqual(transfer_log.summary()["files_received"], 0)

    def test_forgetting_with_no_record_is_harmless(self):
        self.assertEqual(transfer_log.forget_all(), 0)
        self.assertEqual(transfer_log.forget_nick("nicka"), 0)

    def test_forgetting_does_nothing_when_the_record_is_off(self):
        self.sent("A.mp3", nick="nicka")
        self.set_config(TRANSFER_LOG_FILE="")
        self.assertEqual(transfer_log.forget_all(), 0)


class ForgettingReallyRemoves(Case):
    def raw(self):
        """The file and the write-ahead log beside it: both are the record."""
        data = b""
        for suffix in ("", "-wal"):
            if os.path.exists(config.TRANSFER_LOG_FILE + suffix):
                with io.open(config.TRANSFER_LOG_FILE + suffix, "rb") as handle:
                    data += handle.read()
        return data

    def fill(self):
        for n in range(30):
            self.sent(f"UniqueTrackName{n}.mp3", nick="UniqueNickOne")
        transfer_log.record_received("file", 10, nick="UniqueNickOne")
        self.sent("Kept.mp3", nick="othernick")

    def test_a_forgotten_nick_and_its_files_are_gone_from_the_file_itself(self):
        self.fill()
        self.assertIn(b"uniquenickone", self.raw())
        self.assertEqual(transfer_log.forget_nick("UniqueNickOne"), 31)
        raw = self.raw()
        self.assertNotIn(b"uniquenickone", raw)
        self.assertNotIn(b"UniqueTrackName", raw)
        self.assertIn(b"othernick", raw)

    def test_forgetting_everything_leaves_nothing_readable(self):
        self.fill()
        transfer_log.forget_all()
        raw = self.raw()
        for what in (b"uniquenickone", b"othernick", b"UniqueTrackName", b"Kept.mp3"):
            self.assertNotIn(what, raw)

    def test_the_pragma_is_set_before_the_delete_whatever_the_sqlite_build_defaults_to(self):
        # Some SQLite builds zero freed pages by default and some do not, so
        # the bytes above cannot prove the setting; the statements can.
        statements = []

        class Recording(sqlite3.Connection):
            def execute(self, sql, *args):
                statements.append(sql)
                return super().execute(sql, *args)

        real = sqlite3.connect
        self.fill()
        with mock.patch.object(sqlite3, "connect",
                               lambda path, **kw: real(path, factory=Recording, **kw)):
            transfer_log.forget_nick("UniqueNickOne")
            transfer_log.forget_all()
        deletes = [i for i, sql in enumerate(statements) if sql.startswith("DELETE")]
        pragmas = [i for i, sql in enumerate(statements) if sql == "PRAGMA secure_delete = ON"]
        # Two deletes per forget since #1062, the record's rows and the imported
        # ones, in one transaction on one connection: its pragma comes before both.
        self.assertEqual(len(deletes), 4)
        self.assertEqual(len(pragmas), 2)
        self.assertTrue(pragmas[0] < deletes[0] < deletes[1] < pragmas[1] < deletes[2] < deletes[3])

    def test_forgetting_leaves_nothing_in_the_log_beside_the_file_while_the_bot_runs(self):
        """A connection held open for the whole run keeps the WAL from being
        removed on close, so only the checkpoint after the delete empties it."""
        self.fill()
        holder = sqlite3.connect(config.TRANSFER_LOG_FILE)
        self.addCleanup(holder.close)
        holder.execute("SELECT COUNT(*) FROM transfers").fetchall()
        self.assertIn(b"uniquenickone", self.raw())
        transfer_log.forget_nick("UniqueNickOne")
        self.assertNotIn(b"uniquenickone", self.raw())
        self.assertNotIn(b"UniqueTrackName", self.raw())
        self.assertEqual(os.path.getsize(config.TRANSFER_LOG_FILE + "-wal"), 0)

    def test_a_reader_still_open_is_told_about_and_the_rows_go_when_it_finishes(self):
        self.fill()
        reader = sqlite3.connect(config.TRANSFER_LOG_FILE)
        cursor = reader.execute("SELECT * FROM transfers")
        cursor.fetchone()
        out = io.StringIO()
        with mock.patch.object(transfer_log, "WRITE_TIMEOUT", 0.1), contextlib.redirect_stdout(out):
            self.assertEqual(transfer_log.forget_nick("UniqueNickOne"), 31)
        self.assertIn("may still be in", out.getvalue())
        self.assertEqual(transfer_log.nick_summary("UniqueNickOne")["files_sent"], 0)
        cursor.close()
        reader.close()
        transfer_log.forget_all()
        self.assertNotIn(b"uniquenickone", self.raw())

    VICTIM = b"secretvictim"

    def fill_many(self, victims=30, others=600):
        """Enough rows for the table and its indexes to be several pages deep (#1082).

        The rows are built by record_sent() itself, but written in ONE
        transaction on one connection (#1148). Writing them one record_sent()
        at a time reopened the file, re-ran its pragmas and schema and
        checkpointed the log on every close: 18 ms a row, about 11 seconds a
        test. These tests check what forgetting leaves in the file, not the
        writer, which the tests above cover row by row.
        """
        rows = []
        with mock.patch.object(transfer_log, "_record", rows.append):
            for n in range(others):
                self.sent(f"Some Album/Track {n}.flac", nick=f"nick{n % 40}")
                if n % (others // victims) == 0:
                    self.sent(f"Private/Secret {n}.flac", nick="SecretVictim")
        conn = transfer_log._open(config.TRANSFER_LOG_FILE, transfer_log.WRITE_TIMEOUT)
        try:
            with conn:
                conn.executemany(transfer_log._INSERT, rows)
        finally:
            conn.close()

    def test_a_forgotten_nick_is_gone_from_a_record_that_is_many_pages_deep(self):
        self.fill_many()
        self.assertGreater(self.raw().count(self.VICTIM), 0)

        removed = transfer_log.forget_nick("SecretVictim")

        self.assertEqual(removed, 30)
        self.assertEqual(self.raw().count(self.VICTIM), 0)
        self.assertIn(b"nick7", self.raw())
        self.assertEqual(transfer_log.nick_summary("secretvictim")["files_sent"], 0)
        self.assertEqual(len(self.rows()), 600)

    def test_bytes_an_earlier_write_left_in_the_free_space_are_cleaned_too(self):
        """A record written by an older version, or by a SQLite build that does
        not zero by default, holds stale cell copies in the free space of pages
        that still have live rows. The delete only zeroes what it frees itself;
        the rebuild after it is what removes the rest (#1082)."""
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        conn.execute("PRAGMA secure_delete = OFF")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(transfer_log._SCHEMA)
        with conn:
            for n in range(1500):
                nick = "secretvictim" if n % 25 == 0 else f"nick{n % 40}"
                conn.execute("INSERT INTO transfers (direction, nick, kind, ended_at, item_key, name, size,"
                             " bytes, seconds, speed, waited) VALUES ('sent', ?, 'file', ?, ?, ?, 1000, 1000, 1, 1000, NULL)",
                             (nick, n, f"k{n}", f"Name {n}.flac"))
        with conn:
            conn.execute("DELETE FROM transfers WHERE nick = 'secretvictim' AND id % 2 = 1")
        conn.close()
        self.assertGreater(self.raw().count(self.VICTIM), 60)

        transfer_log.forget_nick("SecretVictim")

        self.assertEqual(self.raw().count(self.VICTIM), 0)
        self.assertEqual(len(self.rows()), 1440)

    def test_forgetting_everything_from_a_deep_record_leaves_nothing(self):
        self.fill_many()
        transfer_log.forget_all()
        raw = self.raw()
        for what in (self.VICTIM, b"nick7", b"Some Album", b"Private/Secret"):
            self.assertNotIn(what, raw)

    def test_the_file_is_rebuilt_after_the_delete_and_before_the_log_is_emptied(self):
        statements = []

        class Recording(sqlite3.Connection):
            def execute(self, sql, *args):
                statements.append(sql)
                return super().execute(sql, *args)

        real = sqlite3.connect
        self.fill()
        with mock.patch.object(sqlite3, "connect",
                               lambda path, **kw: real(path, factory=Recording, **kw)):
            transfer_log.forget_nick("UniqueNickOne")
        order = [sql for sql in statements if sql.startswith(("DELETE", "VACUUM", "PRAGMA wal_checkpoint"))]
        # Its imported figures go in the same transaction (#1064), and the file is rebuilt once.
        self.assertEqual(order, ["DELETE FROM transfers WHERE nick = ?", "DELETE FROM imported WHERE nick = ?",
                                 "VACUUM", "PRAGMA wal_checkpoint(TRUNCATE)"])

    def test_every_connection_zeroes_what_it_frees(self):
        statements = []

        class Recording(sqlite3.Connection):
            def execute(self, sql, *args):
                statements.append(sql)
                return super().execute(sql, *args)

        real = sqlite3.connect
        with mock.patch.object(sqlite3, "connect",
                               lambda path, **kw: real(path, factory=Recording, **kw)):
            self.sent("A.mp3", nick="nicka")
            transfer_log.top_nicks()
        self.assertEqual(statements.count("PRAGMA secure_delete = ON"), 2)
        self.assertEqual(statements[0], "PRAGMA secure_delete = ON")

    def test_a_rebuild_that_fails_is_told_and_the_rows_are_still_removed(self):
        self.fill()
        real = sqlite3.connect

        class Failing(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == "VACUUM":
                    raise sqlite3.OperationalError("database or disk is full")
                return super().execute(sql, *args)

        out = io.StringIO()
        with mock.patch.object(sqlite3, "connect", lambda path, **kw: real(path, factory=Failing, **kw)), \
                contextlib.redirect_stdout(out):
            removed = transfer_log.forget_nick("UniqueNickOne")
        self.assertEqual(removed, 31)
        self.assertIn("could not be rebuilt (database or disk is full)", out.getvalue())
        self.assertEqual(transfer_log.nick_summary("UniqueNickOne")["files_sent"], 0)

    def test_a_nick_that_was_also_a_bot_loses_its_received_rows_too(self):
        self.sent("A.mp3", nick="samenick")
        transfer_log.record_received("file", 10, nick="samenick")
        self.assertEqual(transfer_log.forget_nick("samenick"), 2)
        self.assertEqual(self.rows(), [])


class ATransferIsNeverHeldUp(Case):
    def test_a_read_does_not_wait_for_the_write_lock(self):
        self.sent("A.mp3", nick="nicka")
        results = []
        with runtime.transfer_log_lock:
            worker = threading.Thread(target=lambda: results.append(
                (transfer_log.summary()["files_sent"], transfer_log.top_nicks(), transfer_log.top_files())),
                daemon=True)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "a read waited for the lock a send writes under")
        self.assertEqual(results, [(1, [("nicka", 1, 1000)], [("A.mp3", 1)])])

    def test_a_write_waits_a_short_time_for_a_busy_file(self):
        seen = []
        real = sqlite3.connect

        def spy(path, timeout=None, **kw):
            seen.append(timeout)
            return real(path, timeout=timeout, **kw)

        with mock.patch.object(sqlite3, "connect", spy):
            self.sent()
        self.assertEqual(seen, [transfer_log.WRITE_TIMEOUT])
        self.assertLessEqual(transfer_log.WRITE_TIMEOUT, 2)

    def test_a_busy_file_costs_a_send_its_short_wait_and_not_the_transfer(self):
        self.sent()
        blocker = sqlite3.connect(config.TRANSFER_LOG_FILE)
        blocker.execute("BEGIN EXCLUSIVE")
        self.addCleanup(blocker.close)
        began = time.time()
        self.assertFalse(self.sent("B.mp3"))
        self.assertLess(time.time() - began, 5)
        blocker.rollback()
        self.assertEqual(len(self.rows()), 1)


class AReaderNeverStopsAWrite(Case):
    def hold_a_read_open(self):
        for n in range(3):
            self.sent(f"Old{n}.mp3", nick="nicka")
        reader = sqlite3.connect(config.TRANSFER_LOG_FILE)
        cursor = reader.execute("SELECT * FROM transfers")
        cursor.fetchone()
        self.addCleanup(reader.close)
        return reader, cursor

    def test_the_file_is_in_wal_mode(self):
        self.sent()
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

    def test_a_send_that_ends_while_a_query_is_still_reading_is_recorded(self):
        """With the default journal the reader's shared lock stopped the write
        for WRITE_TIMEOUT and the transfer lost its row."""
        reader, cursor = self.hold_a_read_open()
        began = time.time()
        self.assertTrue(self.sent("During.mp3", nick="nickb"))
        self.assertLess(time.time() - began, 1, "the write waited for the reader")
        cursor.close()
        self.assertIn("During.mp3", [row[6] for row in self.rows()])

    def test_the_reader_keeps_its_own_view_and_the_next_one_sees_the_row(self):
        reader, cursor = self.hold_a_read_open()
        self.sent("During.mp3", nick="nickb")
        self.assertEqual(len(cursor.fetchall()), 2, "the open reader sees the file as it was when it began")
        cursor.close()
        self.assertEqual(transfer_log.summary()["files_sent"], 4)

    def test_forgetting_works_while_a_reader_is_open(self):
        reader, cursor = self.hold_a_read_open()
        with mock.patch.object(transfer_log, "WRITE_TIMEOUT", 0.1), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(transfer_log.forget_all(), 3)
        self.assertEqual(transfer_log.summary()["files_sent"], 0)

    def test_an_old_file_in_the_default_journal_mode_is_switched(self):
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        conn.executescript(transfer_log._SCHEMA)
        conn.commit()
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        conn.close()
        self.assertTrue(self.sent("A.mp3"))
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()


class ADamagedFile(Case):
    def damage(self):
        self.sent()
        with io.open(config.TRANSFER_LOG_FILE, "wb") as handle:
            handle.write(b"this is not a database " * 200)

    def aside(self):
        folder = os.path.dirname(config.TRANSFER_LOG_FILE)
        base = os.path.basename(config.TRANSFER_LOG_FILE)
        return [n for n in os.listdir(folder) if n.startswith(base + ".corrupt-")]

    def test_it_is_moved_aside_and_the_next_send_starts_a_new_record(self):
        self.damage()
        self.assertTrue(self.sent("Fresh.mp3", nick="nicka"))
        self.assertEqual(len(self.aside()), 1)
        self.assertEqual(transfer_log.top_files(), [("Fresh.mp3", 1)])

    def test_the_moved_file_is_kept_and_not_deleted(self):
        self.damage()
        self.sent("Fresh.mp3")
        folder = os.path.dirname(config.TRANSFER_LOG_FILE)
        with io.open(os.path.join(folder, self.aside()[0]), "rb") as handle:
            self.assertTrue(handle.read().startswith(b"this is not a database"))

    def test_the_write_ahead_log_of_the_damaged_file_goes_with_it(self):
        """sqlite normally removes its sidecars on close; when it could not, a
        WAL left beside a fresh file would be replayed into it."""
        path = config.TRANSFER_LOG_FILE
        for suffix, body in (("", b"damaged"), ("-wal", b"stale frames"), ("-shm", b"index")):
            with io.open(path + suffix, "wb") as handle:
                handle.write(body)
        aside = transfer_log._move_aside(path)
        for suffix, body in (("", b"damaged"), ("-wal", b"stale frames"), ("-shm", b"index")):
            self.assertFalse(os.path.exists(path + suffix), suffix)
            with io.open(aside + suffix, "rb") as handle:
                self.assertEqual(handle.read(), body)

    def test_a_read_of_a_damaged_file_says_zero_and_does_not_raise(self):
        self.damage()
        self.assertEqual(transfer_log.summary()["files_sent"], 0)
        self.assertEqual(transfer_log.top_nicks(), [])
        self.assertEqual(self.aside(), [], "a read leaves the repair to the next write")

    def damage_past_the_first_page(self):
        """What a bad sector or a torn copy does: the header and the schema
        are intact, so the file opens, and the data pages are not (#1087)."""
        conn = transfer_log._open(config.TRANSFER_LOG_FILE, 5)
        with conn:
            for n in range(600):
                conn.execute("INSERT INTO transfers (direction, nick, kind, ended_at, item_key, name, size,"
                             " bytes, seconds, speed, waited) VALUES ('sent', ?, 'file', ?, ?, ?, 1000, 1000, 1, 1000, NULL)",
                             (f"nick{n % 20}", n, f"k{n}", f"Track {n}.mp3"))
        conn.close()
        with io.open(config.TRANSFER_LOG_FILE, "r+b") as handle:
            handle.seek(4 * 4096)
            size = os.path.getsize(config.TRANSFER_LOG_FILE)
            handle.write(os.urandom(size - 4 * 4096))

    def test_a_file_damaged_past_its_first_page_is_moved_aside_by_the_write_that_hits_it(self):
        self.damage_past_the_first_page()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            written = self.sent("After.mp3", nick="nicka")

        self.assertTrue(written)
        self.assertEqual(len(self.aside()), 1)
        self.assertIn("is damaged", out.getvalue())
        self.assertEqual(transfer_log.top_files(), [("After.mp3", 1)])
        self.assertEqual(len(self.rows()), 1)

    def test_every_write_after_it_goes_into_the_new_file(self):
        self.damage_past_the_first_page()
        for name in ("A.mp3", "B.mp3", "C.mp3"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(self.sent(name))

        self.assertEqual(len(self.aside()), 1)
        self.assertEqual(sorted(name for name, _ in transfer_log.top_files()), ["A.mp3", "B.mp3", "C.mp3"])

    def test_the_damaged_file_is_kept_whole(self):
        self.damage_past_the_first_page()
        before = os.path.getsize(config.TRANSFER_LOG_FILE)
        with contextlib.redirect_stdout(io.StringIO()):
            self.sent("After.mp3")
        folder = os.path.dirname(config.TRANSFER_LOG_FILE)
        self.assertEqual(os.path.getsize(os.path.join(folder, self.aside()[0])), before)

    def test_a_full_disk_on_the_write_does_not_move_a_good_file(self):
        self.sent("Kept.mp3")
        real = sqlite3.connect

        class Full(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("INSERT"):
                    raise sqlite3.OperationalError("database or disk is full")
                return super().execute(sql, *args)

        with mock.patch.object(sqlite3, "connect", lambda path, **kw: real(path, factory=Full, **kw)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.sent("Lost.mp3"))

        self.assertEqual(self.aside(), [])
        self.assertEqual(transfer_log.top_files(), [("Kept.mp3", 1)])

    def test_a_file_that_cannot_be_started_afresh_is_told_and_does_not_raise(self):
        self.damage_past_the_first_page()
        out = io.StringIO()
        with mock.patch.object(transfer_log, "_move_aside", side_effect=OSError("read-only")), \
                contextlib.redirect_stdout(out):
            self.assertFalse(self.sent("After.mp3"))
        self.assertIn("Could not record the transfer", out.getvalue())

    def test_a_file_that_is_only_busy_is_not_moved(self):
        self.sent()
        blocker = sqlite3.connect(config.TRANSFER_LOG_FILE)
        blocker.execute("BEGIN EXCLUSIVE")
        self.addCleanup(blocker.close)
        self.sent("B.mp3")
        self.assertEqual(self.aside(), [])


class TheTopFiles(Case):
    def test_the_most_sent_first_and_ties_by_name(self):
        for name, times in (("B.mp3", 2), ("A.mp3", 2), ("C.mp3", 3), ("D.mp3", 1)):
            for _ in range(times):
                self.sent(name)
        self.assertEqual(transfer_log.top_files(), [("C.mp3", 3), ("A.mp3", 2), ("B.mp3", 2), ("D.mp3", 1)])

    def test_ten_at_most_by_default(self):
        for n in range(12):
            self.sent(f"Track {n:02d}.mp3")
        self.assertEqual(len(transfer_log.top_files()), 10)
        self.assertEqual(len(transfer_log.top_files(limit=3)), 3)

    def test_two_files_with_one_name_in_two_folders_are_two_rows(self):
        self.sent("01.mp3", key="Music/Album One/01.mp3")
        self.sent("01.mp3", key="Music/Album Two/01.mp3")
        self.sent("01.mp3", key="Music/Album Two/01.mp3")
        self.assertEqual(transfer_log.top_files(), [("01.mp3", 2), ("01.mp3", 1)])

    def test_a_list_never_appears(self):
        for _ in range(5):
            self.sent("List-2026-10-01.zip", kind="list")
        self.sent("A.mp3")
        self.assertEqual(transfer_log.top_files(), [("A.mp3", 1)])


class AReceivedFileIsRecorded(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#chan", FETCH_MAX_PER_BOT=0)
        config.channel_users["#chan"] = {"serverone"}
        silence_debug(announce)

    def receive(self, payload, claimed, request_type="file"):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(payload)
        rid = dcc_fetch.enqueue_fetch("ServerOne", "Track.flac", request_type="file")
        row = config.fetch_queue[rid]
        row.update(state="receiving", request_type=request_type)
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        theirs.shutdown(socket.SHUT_WR)
        dcc_fetch._run_transfer(row, {"size": claimed, "ip": None, "port": 0}, dest, "Track.flac",
                                sock=TcpLike(ours))
        return row

    def test_a_finished_download_is_one_row(self):
        row = self.receive(b"x" * 64, 64)
        self.assertEqual(row["state"], "complete")
        figures = transfer_log.summary()
        self.assertEqual((figures["files_received"], figures["bytes_received"]), (1, 64))
        self.assertEqual(transfer_log.top_nicks(transfer_log.RECEIVED), [("serverone", 1, 64)])

    def test_a_download_that_fell_short_is_not(self):
        row = self.receive(b"x" * 32, 64)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(transfer_log.summary()["files_received"], 0)


@unittest.skipUnless(loopback_is_usable(), "this runner cannot bind a listener and dial loopback")
class ASendIsRecorded(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-log-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.served = os.path.join(self.tmp, "Some_Song.mp3")
        with io.open(self.served, "wb") as handle:
            handle.write(CONTENT)
        # A range of its own: a parallel run moves it into this shard's window (#1146).
        port_start, port_end = dcc_ports(51320, 51330)
        self.set_config(
            active_transfers=[{"user": USER, "file": "Some_Song.mp3", "bytes_sent": 0,
                               "next_file_obj": "Some_Song.mp3"}],
            MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8", DCC_PORT_START=port_start, DCC_PORT_END=port_end,
            FILE_DIRECTORY=self.tmp)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        silence_debug(announce)
        self.oserve.total_sent_bytes = 0
        self._real_stall = dcc.ACK_STALL_SECONDS
        dcc.ACK_STALL_SECONDS = 2.0
        self.addCleanup(setattr, dcc, "ACK_STALL_SECONDS", self._real_stall)

    def send(self, ack=True, waited=30):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        next_file = {"path": self.served, "file": "Some_Song.mp3", "channel": CHANNEL,
                     "user_raw": USER, "is_temporary_zip": False, "queued_at": time.time() - waited}
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, USER, self.served, "Some_Song.mp3", CHANNEL, next_file), daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc.handshake_seen.wait(20), "no DCC SEND handshake")
        client = socket.create_connection(("127.0.0.1", irc.port()), timeout=20)
        client.settimeout(20)
        self.addCleanup(client.close)
        held = 0
        while held < len(CONTENT):
            chunk = client.recv(4096)
            if not chunk:
                break
            held += len(chunk)
            if ack:
                client.sendall(struct.pack("!I", held & 0xFFFFFFFF))
        if not ack:
            client.close()
        sender.join(30)

    def test_a_completed_send_is_one_row_with_the_wait_and_the_nick_but_no_host_or_channel(self):
        self.send()
        conn = sqlite3.connect(config.TRANSFER_LOG_FILE)
        rows = conn.execute("SELECT direction, nick, kind, name, size, bytes, waited FROM transfers").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1, rows)
        direction, nick, kind, name, size, wire, waited = rows[0]
        self.assertEqual((direction, nick, kind, name, size, wire),
                         ("sent", USER.lower(), "file", "Some_Song.mp3", len(CONTENT), len(CONTENT)))
        self.assertAlmostEqual(waited, 30, delta=5)
        with io.open(config.TRANSFER_LOG_FILE, "rb") as handle:
            raw = handle.read()
        for what in (CHANNEL, "127.0.0.1"):
            self.assertNotIn(what.encode(), raw)

    def test_a_send_nobody_acknowledged_leaves_no_row(self):
        self.send(ack=False)
        self.assertEqual(transfer_log.summary()["files_sent"], 0)

    def test_a_row_saved_without_a_stamp_has_no_wait(self):
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, USER, self.served, "Some_Song.mp3", CHANNEL, "Some_Song.mp3"), daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc.handshake_seen.wait(20))
        client = socket.create_connection(("127.0.0.1", irc.port()), timeout=20)
        client.settimeout(20)
        self.addCleanup(client.close)
        held = 0
        while held < len(CONTENT):
            held += len(client.recv(4096))
            client.sendall(struct.pack("!I", held & 0xFFFFFFFF))
        sender.join(30)
        self.assertEqual(transfer_log.summary()["files_sent"], 1)
        self.assertIsNone(transfer_log.summary()["queue_wait_seconds"])


class TheQueueStampsWhenAskedFor(unittest.TestCase):
    def test_every_row_the_requests_queue_carries_the_time_it_was_asked_for(self):
        with io.open(os.path.join(support.REPO_ROOT, "src", "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()
        appended = code.count("config.dcc_queue[user_key].append({")
        self.assertEqual(appended, 2)
        self.assertEqual(code.count('"queued_at": time.time()'), 3, "two queued rows and the row sent at once")


if __name__ == "__main__":
    unittest.main()
