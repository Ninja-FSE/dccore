"""A nick whose sends keep failing is paused until midnight (#1389).

MAX_SEND_FAILS counts per queued file, so a nick with twenty files queued
whose client never accepts an offer could cost sixty failed attempts, each
holding a slot for the accept or stall timeout. DAILY_SEND_FAILS_LIMIT (0,
off, by default) counts per nick and per day: the failure that reaches it
removes what is left of the nick's queue with one notice, and the nick's
requests are refused - with one notice per QUEUE_FULL_REPEAT_SECONDS - until
midnight, `unpause <nick>` on the console, or a restart.

What counts is decided where the send fails, so these drive the REAL
start_dcc_send() through every way a send ends: a receiver that never
connected, stalled, let a write time out, or closed before it had the whole
file counts; a file that vanished or shrank on our side, a handshake that
could not go out, and no port do not. The listener and the connection are
fakes, so every runner runs all of it; the send loop is replaced by a
stand-in that ends the way each case needs, except where the real loop is
the point.

Every nick and file name here is invented.
"""

import contextlib
import datetime
import io
import os
import shutil
import socket
import struct
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import runtime  # noqa: E402

from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

USER = "kilo"
FILE = "Some_Album.zip"
CHAN = "#somechannel"
CONTENT = bytes(range(256)) * 40          # 10,240 bytes
TODAY = datetime.date(2026, 3, 4)
TOMORROW = TODAY + datetime.timedelta(days=1)
PAUSED = "paused until midnight"

_REAL_TIMEOUT = socket.timeout


class _Connection:
    """The receiver's end, as accept() hands it over: takes what is sent, or
    resets on the chunk numbered `reset_at` (1-based) when the listener says
    so."""

    reset_at = None

    def __init__(self):
        self.chunks = []

    def settimeout(self, value):
        pass

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        if _Connection.reset_at is not None and len(self.chunks) + 1 == _Connection.reset_at:
            raise ConnectionResetError("reset by peer")
        self.chunks.append(bytes(data))

    def close(self):
        pass


class _Listener:
    """socket.socket for the DCC listener. `behaviour` says what happens:
    "connect" hands over a _Connection, "timeout" waits in vain (without
    the wait), "no_port" refuses every bind."""

    behaviour = "connect"

    def __init__(self, *args, **kwargs):
        pass

    def setsockopt(self, *args):
        pass

    def bind(self, address):
        if _Listener.behaviour == "no_port":
            raise OSError("address already in use")

    def settimeout(self, value):
        pass

    def listen(self, backlog):
        if _Listener.behaviour == "listen_fails":
            raise OSError(24, "Too many open files")

    def accept(self):
        if _Listener.behaviour == "timeout":
            raise _REAL_TIMEOUT("timed out")
        return _Connection(), ("198.51.100.9", 40000)

    def close(self):
        pass


class _Irc:
    """The live IRC socket: records the handshake, or fails to send it."""

    def __init__(self, dead=False):
        self.dead = dead
        self.sent = []

    def sendall(self, data):
        if self.dead:
            raise OSError("Broken pipe")
        self.sent.append(data)

    def send(self, data):
        self.sendall(data)
        return len(data)

    def close(self):
        pass


def raising(exc):
    """A send loop that gets one block out and then raises `exc`."""
    def stream(conn, file_path, resume_offset, bytes_sent, acks, row, user):
        acks.sent = 4096
        raise exc
    return stream


def all_sent_then(ending):
    """A send loop that puts the whole file out, the receiver having
    acknowledged 1,024 bytes of it; `ending(acks)` then says how the receiver
    leaves the final-ack wait: closing, or going quiet."""
    def stream(conn, file_path, resume_offset, bytes_sent, acks, row, user):
        acks.sent = len(CONTENT)
        acks.feed(struct.pack("!I", 1024))
        ending(acks)
        return len(CONTENT)
    return stream


def closes(acks):
    acks.feed(b"")


def goes_quiet(acks):
    acks.stalled = lambda now=None: True


def acking_everything(conn, tracker, wait=0.0):
    tracker.feed(struct.pack("!I", tracker.sent & 0xFFFFFFFF))


class PauseCase(DCCoreTestCase):
    """The limit on, today fixed, the disk and the debug channel stubbed."""

    LIMIT = 1

    def setUp(self):
        super().setUp()
        self.set_config(DAILY_SEND_FAILS_LIMIT=self.LIMIT, MAX_SEND_FAILS=3)
        self.today = TODAY
        patch = mock.patch.object(dcc, "_send_fails_today", lambda: self.today)
        patch.start()
        self.addCleanup(patch.stop)
        self.saves = []
        patch = mock.patch.object(db, "save_dcc_queue", lambda: self.saves.append(1))
        patch.start()
        self.addCleanup(patch.stop)
        self.debug = silence_debug(announce)
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)
        self.out = io.StringIO()

    def count_failures(self, nick, times=1):
        with contextlib.redirect_stdout(self.out):
            for _ in range(times):
                result = dcc.note_failed_send(nick)
        return result

    def paused_notices(self, nick=USER):
        return [m for who, m, *_ in self.oserve.queued if who == nick and PAUSED in m]


# ---------------------------------------------------------------------------
# Through the real start_dcc_send(): what counts and what does not.
# ---------------------------------------------------------------------------

class RealSendCase(PauseCase):
    """One file queued for a send, one more behind it, and the sockets faked."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-pause-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, FILE)
        with io.open(self.path, "wb") as handle:
            handle.write(CONTENT)
        self.set_config(MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8", DCC_ACCEPT_TIMEOUT=45)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        patch = mock.patch.object(announce, "feed_event", lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)
        _Listener.behaviour = "connect"
        _Connection.reset_at = None
        self.addCleanup(setattr, _Connection, "reset_at", None)
        patch = mock.patch("socket.socket", _Listener)
        patch.start()
        self.addCleanup(patch.stop)
        # The row being sent, and one more file waiting behind it.
        self.row = {"file": FILE, "path": self.path, "channel": CHAN,
                    "user_raw": USER, "is_temporary_zip": False}
        self.waiting = {"file": "Other.zip", "path": os.path.join(self.tmp, "Other.zip"),
                        "channel": CHAN, "user_raw": USER, "is_temporary_zip": False}
        config.dcc_queue[USER] = [self.row, self.waiting]
        self.transfer = {"user": USER, "file": FILE, "bytes_sent": 0, "next_file_obj": FILE}
        config.active_transfers[:] = [self.transfer]
        self.oserve.send_fails_count = 0

    def run_the_send(self, stream=None, irc=None):
        irc = irc or _Irc()
        self.oserve.irc_connection = irc
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("time.sleep", lambda *_a, **_k: None))
            stack.enter_context(contextlib.redirect_stdout(self.out))
            if stream is not None:
                stack.enter_context(mock.patch.object(dcc, "stream_the_file", stream))
            dcc.start_dcc_send(irc, USER, self.path, FILE, CHAN, self.row)
        return irc

    def assertCounted(self, how):
        self.assertEqual(dcc.send_fails_paused(USER), 1, how)
        self.assertNotIn(USER, config.dcc_queue, how + ": the pause removes the queue")
        self.assertEqual(len(self.paused_notices()), 1, how)

    def assertNotCounted(self, how):
        self.assertEqual(runtime.failed_transfers, {}, how)
        self.assertEqual(dcc.send_fails_paused(USER), 0, how)
        self.assertEqual(self.paused_notices(), [], how)
        # The sent row kept for retry, as before #1389, and the other untouched.
        self.assertEqual([r["file"] for r in config.dcc_queue.get(USER, [])],
                         [FILE, "Other.zip"], how)


class ThroughARealSend(RealSendCase):

    # --- counted ---------------------------------------------------------

    def test_a_receiver_that_never_connects_counts(self):
        """While some send connected lately: the bot's offers reach people."""
        dcc.note_send_connected()
        _Listener.behaviour = "timeout"
        irc = self.run_the_send()
        self.assertTrue(irc.sent, "the offer went out")
        self.assertCounted("never connected")

    def test_a_receiver_that_closes_mid_file_counts(self):
        """_ReceiverGone: the user's Close cannot be told from a client that
        drops every send, and both count (the decision taken on #1389)."""
        self.run_the_send(raising(dcc._ReceiverGone(1024)))
        self.assertCounted("_ReceiverGone")

    def test_a_receiver_that_stops_acknowledging_counts(self):
        self.run_the_send(raising(dcc._ReceiverStalled(1024)))
        self.assertCounted("_ReceiverStalled")

    def test_a_write_that_times_out_counts(self):
        self.run_the_send(raising(socket.timeout("timed out")))
        self.assertCounted("socket.timeout")

    def test_a_connection_reset_counts(self):
        self.run_the_send(raising(ConnectionResetError("reset by peer")))
        self.assertCounted("ConnectionResetError")

    def test_a_broken_pipe_counts(self):
        self.run_the_send(raising(BrokenPipeError("broken pipe")))
        self.assertCounted("BrokenPipeError")

    def test_a_receiver_that_closes_before_the_final_ack_counts(self):
        self.run_the_send(all_sent_then(closes))
        self.assertCounted("EOF in the final-ack wait")

    def test_a_receiver_that_goes_quiet_before_the_final_ack_counts(self):
        self.run_the_send(all_sent_then(goes_quiet))
        self.assertCounted("stall in the final-ack wait")

    # --- not counted -----------------------------------------------------

    def test_a_file_that_vanished_under_the_read_does_not_count(self):
        real = dcc.stream_the_file

        def vanishing(conn, file_path, *rest):
            os.remove(file_path)
            return real(conn, file_path, *rest)

        self.run_the_send(vanishing)
        self.assertIn("FileNotFoundError", self.out.getvalue())
        self.assertNotCounted("FileNotFoundError")

    def test_a_file_that_got_shorter_under_the_send_does_not_count(self):
        real = dcc.stream_the_file

        def shrinking(conn, file_path, *rest):
            with io.open(file_path, "r+b") as handle:
                handle.truncate(4096)
            return real(conn, file_path, *rest)

        with mock.patch.object(dcc, "_drain_acks", acking_everything):
            self.run_the_send(shrinking)
        self.assertIn("before the file ended", self.out.getvalue())
        self.assertNotCounted("short read")

    def test_a_handshake_that_cannot_go_out_does_not_count(self):
        self.run_the_send(irc=_Irc(dead=True))
        self.assertIn("Failed to send the handshake", self.out.getvalue())
        self.assertNotCounted("handshake")

    def test_a_listener_that_cannot_listen_does_not_count(self):
        """An OSError that is not a ConnectionError - the bot out of
        descriptors - is our side: only a reset or a broken pipe is theirs."""
        dcc.note_send_connected()
        _Listener.behaviour = "listen_fails"
        self.run_the_send()
        self.assertIn("Too many open files", self.out.getvalue())
        self.assertNotCounted("listen() raised")

    def test_no_port_does_not_count(self):
        _Listener.behaviour = "no_port"
        self.run_the_send()
        self.assertEqual(runtime.failed_transfers, {})
        self.assertEqual(dcc.send_fails_paused(USER), 0)
        self.assertEqual([r["file"] for r in config.dcc_queue[USER]], [FILE, "Other.zip"])

    def test_finish_send_counts_only_what_the_send_says_the_receiver_failed(self):
        common = dict(channel=CHAN, irc_sock=_Irc(), outcome_identity=("", FILE, "file"),
                      outcome_status="failed")
        with contextlib.redirect_stdout(self.out), mock.patch("time.sleep", lambda *_a, **_k: None):
            dcc.finish_send(dcc.SentFile(self.transfer, USER, self.row, self.path, FILE, len(CONTENT),
                                         0, 0, False, False, 0.0, 0.0, None,
                                         receiver_failed=False, **common))
            self.assertEqual(runtime.failed_transfers, {})
            config.active_transfers[:] = [self.transfer]
            dcc.finish_send(dcc.SentFile(self.transfer, USER, self.row, self.path, FILE, len(CONTENT),
                                         0, 0, False, False, 0.0, 0.0, None,
                                         receiver_failed=True, **common))
        self.assertEqual(dcc.send_fails_paused(USER), 1)

    # --- the pause, by a real send -----------------------------------------

    def test_the_row_kept_for_retry_by_that_failure_goes_with_the_rest(self):
        self.run_the_send(raising(ConnectionResetError("reset by peer")))
        self.assertNotIn(USER, config.dcc_queue)
        notice = self.paused_notices()[0]
        self.assertIn("failed once today", notice)
        self.assertIn("the 2 files left in your queue were removed", notice)

    def test_under_the_limit_a_failure_is_counted_and_nothing_is_removed(self):
        self.set_config(DAILY_SEND_FAILS_LIMIT=2)
        self.run_the_send(raising(ConnectionResetError("reset by peer")))
        self.assertEqual(runtime.failed_transfers[USER]["fails"], 1)
        self.assertEqual(dcc.send_fails_paused(USER), 0)
        self.assertEqual(len(config.dcc_queue[USER]), 2)
        self.assertEqual(self.paused_notices(), [])

    def test_a_nick_renamed_mid_send_is_counted_under_its_new_name(self):
        """The queue moved with the /nick (irc.note_nick_change()), so the
        count and the pause follow it there."""
        def renamed_then_reset(conn, file_path, resume_offset, bytes_sent, acks, row, user):
            row["user"] = "kilo_away"
            config.dcc_queue["kilo_away"] = config.dcc_queue.pop(USER)
            raise ConnectionResetError("reset by peer")

        self.run_the_send(renamed_then_reset)
        self.assertNotIn(USER, runtime.failed_transfers)
        self.assertEqual(dcc.send_fails_paused("kilo_away"), 1)
        self.assertNotIn("kilo_away", config.dcc_queue)


# ---------------------------------------------------------------------------
# The count and the pause on their own.
# ---------------------------------------------------------------------------

class _FailingFile:
    """The file being sent, failing at `where` ("read": the second read,
    mid-file; "seek": the resume's seek) with `exc` - as a network share
    that hiccups does."""

    def __init__(self, path, where, exc):
        self._real = io.open(path, "rb")
        self._where = where
        self._exc = exc
        self._reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._real.close()
        return False

    def seek(self, offset, *args):
        if self._where == "seek":
            raise self._exc
        return self._real.seek(offset, *args)

    def read(self, size=-1):
        self._reads += 1
        if self._where == "read" and self._reads == 2:
            raise self._exc
        return self._real.read(size)

    def close(self):
        self._real.close()


# The three a network share raises that look like the receiver's: since
# Python 3.10 socket.timeout IS TimeoutError.
SHARE_ERRORS = (TimeoutError, ConnectionResetError, ConnectionAbortedError)


class TheFileFailingIsOurSide(RealSendCase):
    """The real send loop (stream_the_file()), its file failing under it."""

    def setUp(self):
        super().setUp()
        self.set_config(DCC_BLOCK_SIZE=4096)
        self.noted = []
        real_note = dcc.note_failed_send

        def noting(nick):
            self.noted.append(nick)
            return real_note(nick)

        patch = mock.patch.object(dcc, "note_failed_send", noting)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(dcc, "_drain_acks", acking_everything)
        patch.start()
        self.addCleanup(patch.stop)

    def fresh(self):
        """The queue, the slot and the record as setUp left them."""
        self.row["send_fails"] = 0
        config.dcc_queue.clear()
        config.dcc_queue[USER] = [self.row, self.waiting]
        self.transfer["bytes_sent"] = 0
        config.active_transfers[:] = [self.transfer]
        runtime.failed_transfers.clear()
        self.oserve.queued.clear()
        self.noted.clear()
        self.out = io.StringIO()

    def send_with_the_file_failing(self, where, exc):
        path = self.path

        def failing_open(name, mode="r", *args, **kwargs):
            if where == "open":
                raise exc
            return _FailingFile(path, where, exc)

        with mock.patch.object(dcc, "open", failing_open, create=True):
            self.run_the_send()

    def assertOurSide(self, how):
        self.assertIn("could not read the file being sent", self.out.getvalue(), how)
        self.assertEqual(self.noted, [], how + ": note_failed_send() was called")
        self.assertNotCounted(how)

    def check_each_error(self, where, resume_at=0):
        dcc.note_send_connected()
        offer = mock.patch.object(dcc, "clear_send_offer", return_value={"position": resume_at})
        with offer:
            for kind in SHARE_ERRORS:
                with self.subTest(kind=kind.__name__):
                    self.fresh()
                    self.send_with_the_file_failing(where, kind("the share went away"))
                    self.assertOurSide(f"{where}: {kind.__name__}")

    def test_open_failing(self):
        self.check_each_error("open")

    def test_a_read_failing_mid_file(self):
        self.check_each_error("read")

    def test_the_seek_of_a_resume_failing(self):
        self.check_each_error("seek", resume_at=4096)

    def test_a_reset_from_the_receiver_still_counts(self):
        """The control: the same ConnectionResetError, from conn.sendall()."""
        _Connection.reset_at = 2
        self.run_the_send()
        self.assertNotIn("could not read the file being sent", self.out.getvalue())
        self.assertEqual(self.noted, [USER])
        self.assertCounted("ConnectionResetError from sendall")

    def test_it_is_not_an_oserror(self):
        """Were it one, an `except OSError` on the way out - or a handler
        added above its own - would take a share's hiccup for the
        receiver's again."""
        self.assertFalse(issubclass(dcc._SourceUnreadable, OSError))


class TheCircuitBreaker(RealSendCase):
    """A never-connected send counts only when the bot's offers are known to
    work: some send has connected since start-up, and the never-connected
    sends since the last one that connected are not a streak of
    NEVER_CONNECTED_STREAK over NEVER_CONNECTED_STREAK_NICKS nicks or more.
    One downloader whose client never accepts keeps counting; everybody's
    offers going unanswered at once is the bot's address or its port
    forwarding - an ISP reconnect that changed the address - and pauses
    nobody."""

    LIMIT = 5
    SKIPPED = "not counted toward DAILY_SEND_FAILS_LIMIT"

    def never_connected_send(self):
        """A real send to USER whose receiver never connects."""
        _Listener.behaviour = "timeout"
        config.active_transfers[:] = [self.transfer]
        self.run_the_send()

    def never_connected(self, *nicks):
        """finish_send() of a send to each nick in turn whose receiver never
        connected, as start_dcc_send() hands it over."""
        with contextlib.redirect_stdout(self.out), mock.patch("time.sleep", lambda *_a, **_k: None):
            for nick in nicks:
                row = {"user": nick, "file": FILE, "bytes_sent": 0}
                config.active_transfers[:] = [row]
                dcc.finish_send(dcc.SentFile(
                    row, nick, {"file": FILE, "path": self.path}, self.path, FILE, len(CONTENT),
                    0, 0, False, False, 0.0, 0.0, None, channel=CHAN, irc_sock=_Irc(),
                    outcome_identity=("", FILE, "file"), outcome_status="failed",
                    never_connected=True, receiver_failed=True))

    def counts(self):
        return {nick: entry["fails"] for nick, entry in runtime.failed_transfers.items()}

    def skips_logged(self):
        return self.out.getvalue().count(self.SKIPPED)

    def test_the_rule_is_three_sends_over_two_nicks(self):
        self.assertEqual((dcc.NEVER_CONNECTED_STREAK, dcc.NEVER_CONNECTED_STREAK_NICKS), (3, 2))

    def test_not_counted_before_any_send_connected(self):
        self.never_connected_send()
        self.assertNotCounted("no send has connected since start-up")
        self.assertEqual(self.skips_logged(), 1)
        self.assertIn("no send has connected since start-up", self.out.getvalue())
        self.assertIn("the bot's own address (MY_IP_OR_DOCK) or port forwarding", self.out.getvalue())

    def test_one_nick_never_connecting_keeps_counting(self):
        dcc.note_send_connected()
        self.never_connected("alfa", "alfa", "alfa", "alfa")
        self.assertEqual(self.counts(), {"alfa": 4})
        self.assertEqual(self.skips_logged(), 0)

    def test_two_nicks_count_and_a_third_send_to_another_does_not(self):
        dcc.note_send_connected()
        self.never_connected("alfa", "bravo")
        self.assertEqual(self.counts(), {"alfa": 1, "bravo": 1})
        self.never_connected("charlie")
        self.assertEqual(self.counts(), {"alfa": 1, "bravo": 1})
        self.assertEqual(self.skips_logged(), 1)
        self.assertIn("3 offers to 3 different nicks have gone unanswered", self.out.getvalue())

    def test_three_to_one_nick_then_another_is_not_counted(self):
        dcc.note_send_connected()
        self.never_connected("alfa", "alfa", "alfa")
        self.assertEqual(self.counts(), {"alfa": 3})
        self.never_connected("bravo")
        self.assertEqual(self.counts(), {"alfa": 3})
        self.assertEqual(self.skips_logged(), 1)

    def test_one_nick_in_any_case_is_one_nick(self):
        dcc.note_send_connected()
        self.never_connected("Delta", "DELTA", "delta", "dElTa")
        self.assertEqual(self.counts(), {"delta": 4})
        self.assertEqual(self.skips_logged(), 0)

    def test_a_fourth_send_logs_nothing_new(self):
        dcc.note_send_connected()
        self.never_connected("alfa", "bravo", "charlie")
        self.assertEqual(self.skips_logged(), 1)
        self.never_connected("delta", "echo")
        self.assertEqual(self.skips_logged(), 1)
        self.assertEqual(self.counts(), {"alfa": 1, "bravo": 1})

    def test_a_connect_rearms_counting_and_the_log_line(self):
        dcc.note_send_connected()
        self.never_connected("alfa", "bravo", "charlie")
        self.assertEqual(self.skips_logged(), 1)
        dcc.note_send_connected()
        # A fresh streak: one nick three times counts all three ...
        self.never_connected("delta", "delta", "delta")
        self.assertEqual(self.counts()["delta"], 3)
        self.assertEqual(self.skips_logged(), 1)
        # ... and the next streak of several nicks is said again.
        self.never_connected("echo")
        self.assertNotIn("echo", self.counts())
        self.assertEqual(self.skips_logged(), 2)

    def test_a_receiver_that_failed_after_connecting_counts_with_no_connect_on_record(self):
        """Only a never-connected send needs the breaker: a stall or a close
        mid-file came from a receiver that did connect."""
        common = dict(channel=CHAN, irc_sock=_Irc(), outcome_identity=("", FILE, "file"),
                      outcome_status="failed", receiver_failed=True, never_connected=False)
        self.set_config(DAILY_SEND_FAILS_LIMIT=1)
        self.assertFalse(runtime.send_connected_since_start)
        with contextlib.redirect_stdout(self.out), mock.patch("time.sleep", lambda *_a, **_k: None):
            dcc.finish_send(dcc.SentFile(self.transfer, USER, self.row, self.path, FILE, len(CONTENT),
                                         0, 4096, False, False, 0.0, 0.0, None, **common))
        self.assertCounted("stalled or gone, no connect on record")
        self.assertEqual(self.skips_logged(), 0)

    def test_a_stalled_and_a_closed_send_count_in_a_real_send(self):
        self.set_config(DAILY_SEND_FAILS_LIMIT=1)
        for name, exc in (("_ReceiverStalled", dcc._ReceiverStalled(1024)),
                          ("_ReceiverGone", dcc._ReceiverGone(1024))):
            with self.subTest(name=name):
                runtime.failed_transfers.clear()
                config.dcc_queue[USER] = [self.row]
                config.active_transfers[:] = [self.transfer]
                self.oserve.queued.clear()
                self.run_the_send(raising(exc))
                self.assertEqual(dcc.send_fails_paused(USER), 1, name)
                self.assertEqual(self.skips_logged(), 0)

    def test_nothing_is_logged_with_the_limit_off(self):
        self.set_config(DAILY_SEND_FAILS_LIMIT=0)
        self.never_connected_send()
        self.never_connected("alfa", "bravo", "charlie")
        self.assertEqual(self.skips_logged(), 0)
        self.assertEqual(runtime.never_connected_streak, 0)
        self.assertFalse(runtime.never_connected_streak_logged)

    def test_a_send_whose_receiver_connects_arms_it_and_ends_the_streak(self):
        runtime.never_connected_streak = 2
        runtime.never_connected_streak_nicks.update({"alfa", "bravo"})
        runtime.never_connected_streak_logged = True
        self.run_the_send(raising(ConnectionResetError("reset by peer")))
        self.assertTrue(runtime.send_connected_since_start)
        self.assertEqual(runtime.never_connected_streak, 0)
        self.assertEqual(runtime.never_connected_streak_nicks, set())
        self.assertFalse(runtime.never_connected_streak_logged)

    def test_a_send_nobody_connected_to_only_lengthens_the_streak(self):
        self.never_connected_send()
        self.assertFalse(runtime.send_connected_since_start)
        self.assertEqual(runtime.never_connected_streak, 1)
        self.assertEqual(runtime.never_connected_streak_nicks, {USER})

    def test_an_isp_reconnect_pauses_nobody(self):
        """Sends connect; then the address changes under the bot, and every
        one of twenty queued nicks' offers goes unanswered, each tried
        MAX_SEND_FAILS times, with a limit of 3. Nobody is paused."""
        self.set_config(DAILY_SEND_FAILS_LIMIT=3, MAX_SEND_FAILS=3)
        # A send that connects: the receiver takes it, our file comes up
        # short (our side, not counted against anybody).
        real = dcc.stream_the_file

        def shrinking(conn, file_path, *rest):
            with io.open(file_path, "r+b") as handle:
                handle.truncate(4096)
            return real(conn, file_path, *rest)

        with mock.patch.object(dcc, "_drain_acks", acking_everything):
            self.run_the_send(shrinking)
        self.assertTrue(runtime.send_connected_since_start)
        with io.open(self.path, "wb") as handle:
            handle.write(CONTENT)

        nicks = ["nick%02d" % n for n in range(1, 21)]
        rows = {}
        for nick in nicks:
            rows[nick] = {"file": FILE, "path": self.path, "channel": CHAN,
                          "user_raw": nick, "is_temporary_zip": False}
            config.dcc_queue[nick] = [rows[nick]]
        _Listener.behaviour = "timeout"
        irc = _Irc()
        self.oserve.irc_connection = irc
        with contextlib.redirect_stdout(self.out), mock.patch("time.sleep", lambda *_a, **_k: None):
            for _round in range(3):
                for nick in nicks:
                    if rows[nick] not in config.dcc_queue.get(nick, []):
                        continue
                    config.active_transfers[:] = [{"user": nick, "file": FILE, "bytes_sent": 0,
                                                   "next_file_obj": FILE}]
                    dcc.start_dcc_send(irc, nick, self.path, FILE, CHAN, rows[nick])

        self.assertEqual(dcc.paused_for_send_fails(), {})
        self.assertTrue(all(dcc.send_fails_paused(nick) == 0 for nick in nicks))
        self.assertEqual(self.paused_notices("nick01") + self.paused_notices("nick02"), [])
        self.assertEqual(self.counts(), {"nick01": 1, "nick02": 1},
                         "only the two sends before the streak was a streak count")
        self.assertEqual(self.skips_logged(), 1)
        self.assertEqual(runtime.never_connected_streak, 60)


class AListRequestFromAPausedNick(feed.ServesARealRequest):
    """`!list` (list.send_file_list()): its "Preparing full list" notice is
    sent before handle_download_request() would refuse the archive."""

    def setUp(self):
        super().setUp()
        self.set_config(DAILY_SEND_FAILS_LIMIT=2)
        patch = mock.patch.object(dcc, "_send_fails_today", lambda: TODAY)
        patch.start()
        self.addCleanup(patch.stop)
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)
        # list.py binds oserve at import; the notices of this test are the stub's.
        patch = mock.patch.object(list_mod, "oserve", self.oserve)
        patch.start()
        self.addCleanup(patch.stop)
        self.downloads = []
        patch = mock.patch.object(dcc, "handle_download_request",
                                  lambda *args: self.downloads.append(args))
        patch.start()
        self.addCleanup(patch.stop)
        # A published list, so the request gets as far as "Preparing" when it is let.
        with io.open(os.path.join(self.tree.lists, "DCCoreTest-2026-03-04.zip"), "wb") as handle:
            handle.write(b"PK\x05\x06" + b"\x00" * 18)

    def pause(self, nick="dave"):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.note_failed_send(nick)
            dcc.note_failed_send(nick)
        self.oserve.queued.clear()

    def ask_for_the_list(self, user="dave"):
        with contextlib.redirect_stdout(io.StringIO()):
            list_mod.send_file_list(self.sock, user, feed.OTHER)

    def told(self, user="dave"):
        return [m for who, m, *_ in self.oserve.queued if who == user]

    def test_a_paused_nick_gets_the_pause_notice_only(self):
        self.pause()
        self.ask_for_the_list()
        told = self.told()
        self.assertEqual(len(told), 1, told)
        self.assertIn(PAUSED, told[0])
        self.assertIn("failed 2 times today", told[0])
        self.assertNotIn("Preparing", " ".join(told))
        self.assertEqual(self.downloads, [])

    def test_asking_again_inside_the_window_gets_nothing(self):
        self.pause()
        self.ask_for_the_list()
        self.ask_for_the_list()
        self.assertEqual(len(self.told()), 1)
        self.assertEqual(self.downloads, [])

    def test_a_nick_that_is_not_paused_is_sent_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.note_failed_send("dave")
        self.ask_for_the_list()
        told = self.told()
        self.assertTrue(any("Preparing full list" in m for m in told), told)
        self.assertFalse(any(PAUSED in m for m in told), told)
        self.assertEqual(len(self.downloads), 1)


class OffByDefault(DCCoreTestCase):

    def test_the_shipped_value_is_off(self):
        self.assertEqual(config.DAILY_SEND_FAILS_LIMIT, 0)
        self.assertEqual(dcc.daily_send_fails_limit(), 0)


class TheLimit(PauseCase):

    def test_anything_that_is_not_a_positive_number_is_off(self):
        for value, expected in ((5, 5), ("4", 4), (0, 0), (-3, 0), (None, 0), ("lots", 0), ([], 0)):
            with self.subTest(value=value):
                self.set_config(DAILY_SEND_FAILS_LIMIT=value)
                self.assertEqual(dcc.daily_send_fails_limit(), expected)

    def test_with_the_limit_off_nothing_is_counted(self):
        self.set_config(DAILY_SEND_FAILS_LIMIT=0)
        self.assertEqual(self.count_failures(USER, 5), 0)
        self.assertEqual(runtime.failed_transfers, {})
        self.assertEqual(dcc.send_fails_paused(USER), 0)
        self.assertEqual(dcc.paused_for_send_fails(), {})


class ThePause(PauseCase):

    LIMIT = 3

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        self.archive = os.path.join(config.TMP_ZIP_DIR, "Some_Folder.rar")
        with io.open(self.archive, "wb") as handle:
            handle.write(b"Rar!")
        config.dcc_queue[USER] = [
            {"file": "Some_Folder.rar", "path": self.archive, "is_temporary_zip": True, "user_raw": USER},
            {"file": "Song.flac", "path": os.path.join(self.tree.music, "Song.flac"),
             "is_temporary_zip": False, "user_raw": USER}]
        config.dcc_queue["lima"] = [{"file": "Other.flac", "path": "x", "is_temporary_zip": False}]
        config.frozen_queues[USER] = 12345.0

    def test_under_the_limit_nothing_happens_but_the_count(self):
        self.assertEqual(self.count_failures(USER, 2), 2)
        self.assertEqual(dcc.send_fails_paused(USER), 0)
        self.assertEqual(len(config.dcc_queue[USER]), 2)
        self.assertTrue(os.path.exists(self.archive))
        self.assertEqual(self.paused_notices(), [])

    def test_exactly_the_limit_pauses(self):
        self.assertEqual(self.count_failures(USER, 3), 3)
        self.assertEqual(dcc.send_fails_paused(USER), 3)
        self.assertEqual(dcc.paused_for_send_fails(), {USER: 3})

    def test_the_pause_removes_the_queue_its_archive_and_its_freeze(self):
        self.count_failures(USER, 3)
        self.assertNotIn(USER, config.dcc_queue)
        self.assertFalse(os.path.exists(self.archive), "the orphaned temp archive is deleted")
        self.assertNotIn(USER, config.frozen_queues)
        self.assertTrue(self.saves, "the queue is saved")
        self.assertEqual(len(config.dcc_queue["lima"]), 1, "another nick's queue is left alone")

    def test_one_notice_with_the_count_and_the_files_removed(self):
        self.count_failures(USER, 3)
        notices = self.paused_notices()
        self.assertEqual(len(notices), 1, notices)
        self.assertTrue(notices[0].startswith(f"NOTICE {USER} :"))
        self.assertIn("failed 3 times today", notices[0])
        self.assertIn("the 2 files left in your queue were removed", notices[0])
        self.assertIn("Check that your client accepts DCC sends", notices[0])

    def test_the_operator_is_told_too(self):
        self.count_failures(USER, 3)
        said = [text for _cat, text in self.debug if "paused until midnight" in text]
        self.assertEqual(len(said), 1, self.debug)
        self.assertIn("2 queued file(s) removed", said[0])
        self.assertIn(f"unpause {USER}", said[0])

    def test_a_failure_after_the_pause_sends_no_second_notice(self):
        self.count_failures(USER, 3)
        self.assertEqual(self.count_failures(USER), 4)
        self.assertEqual(len(self.paused_notices()), 1)
        self.assertEqual(dcc.send_fails_paused(USER), 4)

    def test_a_nick_with_nothing_queued_is_still_told(self):
        config.dcc_queue.pop(USER)
        self.count_failures(USER, 3)
        notices = self.paused_notices()
        self.assertEqual(len(notices), 1)
        self.assertNotIn("removed", notices[0])

    def test_a_limit_raised_past_the_count_and_lowered_again_pauses_again(self):
        self.count_failures(USER, 3)
        self.set_config(DAILY_SEND_FAILS_LIMIT=10)
        self.count_failures(USER)
        self.assertEqual(dcc.send_fails_paused(USER), 0)
        self.set_config(DAILY_SEND_FAILS_LIMIT=3)
        self.count_failures(USER)
        self.assertEqual(len(self.paused_notices()), 2)


class OneCountPerNick(PauseCase):

    LIMIT = 3

    def test_every_case_of_a_nick_shares_one_count(self):
        self.count_failures("Alice")
        self.count_failures("ALICE")
        self.assertEqual(self.count_failures("alice"), 3)
        self.assertEqual(dcc.send_fails_paused("aLiCe"), 3)
        self.assertEqual(dcc.paused_for_send_fails(), {"alice": 3})

    def test_the_pause_removes_the_queue_under_the_lowercased_key(self):
        config.dcc_queue["alice"] = [{"file": "a.flac", "path": "a", "is_temporary_zip": False}]
        self.count_failures("Alice", 3)
        self.assertNotIn("alice", config.dcc_queue)
        self.assertEqual(len(self.paused_notices("Alice")), 1)

    def test_another_nick_has_its_own_count(self):
        self.count_failures("alice", 3)
        self.assertEqual(dcc.send_fails_paused("bravo"), 0)
        self.assertEqual(self.count_failures("bravo"), 1)


class Midnight(PauseCase):

    LIMIT = 2

    def test_a_pause_ends_when_the_day_does(self):
        self.count_failures(USER, 2)
        self.assertEqual(dcc.send_fails_paused(USER), 2)
        self.today = TOMORROW
        self.assertEqual(dcc.send_fails_paused(USER), 0)
        self.assertEqual(dcc.paused_for_send_fails(), {})

    def test_the_first_failure_of_a_new_day_starts_from_one(self):
        self.count_failures(USER, 2)
        self.today = TOMORROW
        self.assertEqual(self.count_failures(USER), 1)
        self.assertEqual(dcc.send_fails_paused(USER), 0)

    def test_and_it_drops_every_count_from_the_day_before(self):
        self.count_failures(USER, 2)
        self.count_failures("lima")
        self.today = TOMORROW
        self.count_failures("mike")
        self.assertEqual(set(runtime.failed_transfers), {"mike"})

    def test_a_new_day_can_pause_the_same_nick_again(self):
        self.count_failures(USER, 2)
        self.today = TOMORROW
        self.count_failures(USER, 2)
        self.assertEqual(len(self.paused_notices()), 2)


class AdminsAreNeverPaused(PauseCase):

    LIMIT = 2

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_NICK="SysOp, Boss")

    def test_an_admin_is_not_counted(self):
        self.assertEqual(self.count_failures("BOSS", 5), 0)
        self.assertEqual(runtime.failed_transfers, {})
        self.assertEqual(self.paused_notices("BOSS"), [])

    def test_an_admin_is_not_paused_whatever_the_count_says(self):
        runtime.failed_transfers["boss"] = {"day": TODAY, "fails": 9, "paused": True}
        self.assertEqual(dcc.send_fails_paused("Boss"), 0)
        self.assertEqual(dcc.paused_for_send_fails(), {})

    def test_a_nick_that_is_not_an_admin_is(self):
        self.count_failures("bossy", 2)
        self.assertEqual(dcc.send_fails_paused("bossy"), 2)


class Unpausing(PauseCase):

    LIMIT = 2

    def test_it_forgets_the_count_and_says_how_many_there_were(self):
        self.count_failures(USER, 3)
        self.assertEqual(dcc.unpause_send_fails("KILO"), 3)
        self.assertEqual(dcc.send_fails_paused(USER), 0)
        self.assertNotIn(USER, runtime.failed_transfers)
        self.assertEqual(self.count_failures(USER), 1, "counting starts again from one")

    def test_a_nick_with_nothing_counted_has_nothing_to_lift(self):
        self.assertEqual(dcc.unpause_send_fails("nobody"), 0)

    def test_a_count_from_yesterday_is_nothing_to_lift(self):
        self.count_failures(USER, 2)
        self.today = TOMORROW
        self.assertEqual(dcc.unpause_send_fails(USER), 0)


# ---------------------------------------------------------------------------
# What the nick is told, and the admin nicks.
# ---------------------------------------------------------------------------

class TheWords(unittest.TestCase):

    def test_the_refusal_names_the_count_and_what_to_check(self):
        text = announce.send_fails_paused_text(4)
        self.assertEqual(text, "Your downloads failed 4 times today, so requests from you are paused "
                               "until midnight. Check that your client accepts DCC sends.")

    def test_the_pause_names_the_files_removed_in_the_right_number(self):
        self.assertIn(", and the 1 file left in your queue was removed.",
                      announce.send_fails_paused_text(3, 1))
        self.assertIn(", and the 5 files left in your queue were removed.",
                      announce.send_fails_paused_text(3, 5))

    def test_one_and_more_than_one_both_read_right(self):
        """'failed 1 times' and '1 file ... were removed' were the first wording."""
        head = "Your downloads failed {} today, so requests from you are paused until midnight"
        tail = ". Check that your client accepts DCC sends."
        for fails, times in ((1, "once"), (2, "2 times")):
            for removed, files in ((0, ""),
                                   (1, ", and the 1 file left in your queue was removed"),
                                   (2, ", and the 2 files left in your queue were removed")):
                with self.subTest(fails=fails, removed=removed):
                    self.assertEqual(announce.send_fails_paused_text(fails, removed),
                                     head.format(times) + files + tail)


class TheRefusalNotice(PauseCase):

    def test_it_is_said_once_per_repeat_window(self):
        for _ in range(5):
            announce.send_fails_paused_notice(USER, 3, CHAN)
        self.assertEqual(len(self.paused_notices()), 1)

    def test_and_again_once_the_window_has_passed(self):
        announce.send_fails_paused_notice(USER, 3, CHAN)
        key = (USER, "send_fails_paused")
        announce._told_queue_full[key] -= announce.QUEUE_FULL_REPEAT_SECONDS + 1
        announce.send_fails_paused_notice(USER, 3, CHAN)
        self.assertEqual(len(self.paused_notices()), 2)

    def test_nothing_in_a_request_only_channel(self):
        with mock.patch.object(library, "mode_for_request", return_value=library.REQUEST_ONLY):
            announce.send_fails_paused_notice(USER, 3, CHAN)
        self.assertEqual(self.paused_notices(), [])


class AdminNicks(DCCoreTestCase):

    def test_the_nicks_of_admin_nick_lowercased(self):
        self.set_config(ADMIN_NICK=" SysOp ,Boss,, ")
        self.assertEqual(commands.admin_nicks(), {"sysop", "boss"})

    def test_none_when_the_setting_is_blank_or_absent(self):
        for value in ("", None):
            with self.subTest(value=value):
                self.set_config(ADMIN_NICK=value)
                self.assertEqual(commands.admin_nicks(), set())

    def test_is_admin_still_refuses_a_nick_outside_it(self):
        self.set_config(ADMIN_NICK="SysOp")
        self.assertTrue(commands.is_admin("SYSOP"))
        self.assertFalse(commands.is_admin("someone"))


# ---------------------------------------------------------------------------
# A paused nick's request.
# ---------------------------------------------------------------------------

class ARequestFromAPausedNick(feed.ServesARealRequest):

    def setUp(self):
        super().setUp()
        self.set_config(DAILY_SEND_FAILS_LIMIT=2)
        patch = mock.patch.object(dcc, "_send_fails_today", lambda: TODAY)
        patch.start()
        self.addCleanup(patch.stop)
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)
        # Slots full, so a request that is taken queues rather than sends.
        self.fill_the_slots()

    def pause(self, nick="dave"):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.note_failed_send(nick)
            dcc.note_failed_send(nick)
        self.oserve.queued.clear()

    def ask(self, what="Song.flac", user="dave", channel=feed.OTHER):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, user, what, channel)
        return out.getvalue()

    def rows(self, user="dave"):
        return [row["file"] for row in config.dcc_queue.get(user, [])]

    def refusals(self, user="dave"):
        return [m for who, m, *_ in self.oserve.queued if who == user and PAUSED in m]

    def test_a_nick_that_is_not_paused_is_queued(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dcc.note_failed_send("dave"), 1)
        self.ask()
        self.assertEqual(self.rows(), ["Song.flac"])
        self.assertEqual(self.refusals(), [])

    def test_a_paused_nick_is_not_queued_and_is_told_why(self):
        self.pause()
        out = self.ask()
        self.assertEqual(self.rows(), [])
        self.assertNotIn("dave", config.dcc_queue)
        told = self.refusals()
        self.assertEqual(len(told), 1, self.oserve.queued)
        self.assertIn("failed 2 times today", told[0])
        self.assertIn("Refused a request from dave", out)

    def test_it_is_refused_before_any_lookup(self):
        self.pause()
        out = self.ask()
        self.assertNotIn("REQUEST", self.kinds())
        self.assertNotIn("dave requested", out)

    def test_a_folder_request_is_refused_the_same(self):
        self.pause()
        self.ask("!rar Metallica/Black Album (1991)")
        self.assertNotIn("dave", config.dcc_queue)
        self.assertEqual(len(self.refusals()), 1)

    def test_a_paste_of_many_requests_is_told_once(self):
        self.pause()
        for _ in range(30):
            self.ask()
        self.assertEqual(len(self.refusals()), 1)
        self.assertEqual(self.rows(), [])

    def test_a_request_only_channel_gets_no_notice_and_no_row(self):
        self.pause()
        with mock.patch.object(library, "mode_for_request", return_value=library.REQUEST_ONLY):
            self.ask()
        self.assertEqual(self.refusals(), [])
        self.assertEqual(self.rows(), [])

    def test_once_unpaused_the_request_is_queued(self):
        self.pause()
        dcc.unpause_send_fails("Dave")
        self.ask()
        self.assertEqual(self.rows(), ["Song.flac"])

    def test_another_nick_is_queued_as_usual(self):
        config.channel_users[feed.OTHER].add("erin")
        self.pause("dave")
        self.ask(user="erin")
        self.assertEqual(self.rows("erin"), ["Song.flac"])

    def test_an_admin_is_queued_whatever_the_count(self):
        self.set_config(ADMIN_NICK="Dave")
        runtime.failed_transfers["dave"] = {"day": TODAY, "fails": 9, "paused": True}
        self.ask()
        self.assertEqual(self.rows(), ["Song.flac"])


# ---------------------------------------------------------------------------
# The console.
# ---------------------------------------------------------------------------

class _Session:
    """A console session that records instead of writing a socket."""

    def __init__(self, read_only=False):
        self.nick = "SysOp"
        self.host = "sysop.example.org"
        self.peer_ip = "127.0.0.1"
        self.authenticated = True
        self.unlocked = True
        self.closed = False
        self.structured = False
        self.read_only = read_only
        self.paired_as = "widget" if read_only else None
        self.lines = []

    def send(self, text=""):
        self.lines.append(str(text))

    def close(self, announce_text=None):
        self.closed = True


class TheConsole(PauseCase):

    LIMIT = 2

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(HARD_BANS_FILE=os.path.join(tree.root, "hard_bans.txt"))

    def run_command(self, line, session=None):
        session = session or _Session()
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_command(session, line)
        return session.lines

    def paused_lines(self):
        return [line for line in self.run_command("status") if line.startswith("Paused")]

    def test_status_has_no_paused_line_with_the_limit_off(self):
        self.set_config(DAILY_SEND_FAILS_LIMIT=0)
        self.assertEqual(self.paused_lines(), [])

    def test_status_says_nobody_is_paused(self):
        self.count_failures(USER)
        self.assertEqual(self.paused_lines(), ["Paused      : nobody for failed sends today"])

    def test_status_names_each_paused_nick_with_its_count(self):
        self.count_failures("lima", 3)
        self.count_failures(USER, 2)
        self.count_failures("mike")
        self.assertEqual(self.paused_lines(), [
            "Paused      : 2 nick(s) until midnight for failed sends today: "
            "kilo (2), lima (3). unpause <nick> lifts it."])

    def test_queue_of_a_paused_nick_says_so(self):
        self.count_failures(USER, 2)
        self.assertIn("kilo has nothing queued - paused until midnight after 2 failed sends today.",
                      self.run_command("queue kilo"))

    def test_queue_of_a_nick_that_is_not_paused_says_nothing_of_it(self):
        self.count_failures(USER)
        self.assertIn("kilo has nothing queued.", self.run_command("queue kilo"))

    def test_unpause_lifts_it(self):
        self.count_failures(USER, 2)
        lines = self.run_command("unpause Kilo")
        self.assertEqual(lines, ["Kilo's 2 failed send(s) today are forgotten; its requests are taken again."])
        self.assertEqual(dcc.send_fails_paused(USER), 0)
        self.assertEqual(self.paused_lines(), ["Paused      : nobody for failed sends today"])

    def test_unpause_of_a_nick_with_nothing_counted(self):
        self.assertEqual(self.run_command("unpause kilo"),
                         ["kilo has no failed sends counted today; nothing to lift."])

    def test_unpause_needs_exactly_one_nick(self):
        self.count_failures(USER, 2)
        for line in ("unpause", "unpause   ", "unpause kilo lima"):
            with self.subTest(line=line):
                self.assertEqual(self.run_command(line), ["Usage: unpause <nick>"])
        self.assertEqual(dcc.send_fails_paused(USER), 2)

    def test_a_read_only_token_cannot_unpause(self):
        """#1374: unpause changes something, so it is not on the allow-list."""
        self.count_failures(USER, 2)
        self.assertIn("unpause", adminchat.COMMANDS)
        self.assertNotIn("unpause", adminchat.READ_ONLY_COMMANDS | adminchat.READ_ONLY_REPORTS)
        lines = self.run_command("unpause kilo", _Session(read_only=True))
        self.assertEqual(len(lines), 1)
        self.assertIn("read-only token", lines[0])
        self.assertEqual(dcc.send_fails_paused(USER), 2)


if __name__ == "__main__":
    unittest.main()
