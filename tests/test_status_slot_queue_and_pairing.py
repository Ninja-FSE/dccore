"""#550, step 3: the live picture, and a credential that is not the password.

STATUS/SLOT/QUEUE are what the client's title bar and side panel are drawn
from - slots in use, the queue in order with frozen countdowns, today's
totals, the record - sent every STATUS_INTERVAL on the session's own writer
thread (also the heartbeat) and after any event that moved a slot or the
queue.

`pair` mints a token a script can log in with; only its PBKDF2 hash is
kept. A token opens a chat and nothing else: the dashboard's login checks
ADMIN_PASSWORD_HASH alone. `unpair` lists or revokes.
"""

import io
import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import db  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_adminchat import (  # noqa: E402
    ADMIN_LINE, LOOPBACK_OK, NEEDS_LOOPBACK, PASSWORD, wait_for)


class TheStatusBurst(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(MAX_DCC_SLOTS=3)
        config.active_transfers.clear()
        config.dcc_queue.clear()
        config.frozen_queues.clear()
        import stats_mgr
        self._real_speed = stats_mgr.live_speed
        stats_mgr.live_speed = lambda now=None: 1500000
        self.addCleanup(setattr, stats_mgr, "live_speed", self._real_speed)
        self._real_rolled = db.load_advanced_stats_rolled
        db.load_advanced_stats_rolled = lambda: [100, 999, 10, 1000, 38, 13300000000, "2026-09-18"]
        self.addCleanup(setattr, db, "load_advanced_stats_rolled", self._real_rolled)
        # Since the bot started (#754): 100 s of uptime at now=1000, and the
        # counts the daemon has kept.
        self._real_uptime = stats_mgr.get_uptime_seconds
        stats_mgr.get_uptime_seconds = lambda: 100
        self.addCleanup(setattr, stats_mgr, "get_uptime_seconds", self._real_uptime)
        import runtime
        self._real_counts = dict(runtime.feed_counts)
        runtime.feed_counts.update({"FAIL": 4, "SEARCH": 7})
        self.addCleanup(lambda: runtime.feed_counts.update(self._real_counts))
        self._real_record = db.get_speed_record
        db.get_speed_record = lambda: 4800000
        self.addCleanup(setattr, db, "get_speed_record", self._real_record)

    def test_status_carries_the_title_bar_figures(self):
        config.active_transfers[:] = [{"user": "erin", "file": "X.rar", "bytes_sent": 1}]
        config.dcc_queue.update({"helen": [1, 2, 3, 4], "dave": [1]})

        lines = adminchat.status_lines(now=1000.0)

        self.assertEqual(lines[0], "DCCORE STATUS 1 3 5 2 38 13300000000 1500000 4800000 900 4 7")

    def test_a_slot_line_per_transfer_with_speed_from_its_own_clock(self):
        config.active_transfers[:] = [{"user": "erin", "file": "X Y.rar", "bytes_sent": 5000,
                                       "size": 10000, "started_at": 998.0}]

        lines = adminchat.status_lines(now=1000.0)

        self.assertEqual(lines[1], "DCCORE SLOT erin 5000 10000 2500 X Y.rar")

    def test_a_resumed_transfer_counts_only_what_this_connection_moved(self):
        """Resumed at 20 GB, five seconds in, 30 MB sent since: 6 MB/s - not
        20 GB / 5 s, which read 4 GB/s and, a little later, 108 MB/s."""
        resumed_at = 20 * 1024 ** 3
        sent = resumed_at + 30_000_000
        config.active_transfers[:] = [{"user": "erin", "file": "X.mkv", "bytes_sent": sent,
                                       "size": 24 * 1024 ** 3, "started_at": 995.0,
                                       "resume_offset": resumed_at}]

        line = adminchat.status_lines(now=1000.0)[1]

        self.assertEqual(line, f"DCCORE SLOT erin {sent} {24 * 1024 ** 3} 6000000 X.mkv")

    def test_the_progress_still_counts_from_the_start_of_the_file(self):
        """Only the speed changes: sent/total stays the receiver's whole copy."""
        config.active_transfers[:] = [{"user": "e", "file": "x", "bytes_sent": 900, "size": 1000,
                                       "started_at": 990.0, "resume_offset": 800}]
        fields = adminchat.status_lines(now=1000.0)[1].split()
        self.assertEqual(fields[3:5], ["900", "1000"])

    def test_a_row_without_a_resume_offset_is_as_before(self):
        config.active_transfers[:] = [{"user": "erin", "file": "X.rar", "bytes_sent": 5000,
                                       "size": 10000, "started_at": 998.0}]
        self.assertEqual(adminchat.status_lines(now=1000.0)[1], "DCCORE SLOT erin 5000 10000 2500 X.rar")

    def test_a_resume_that_has_moved_nothing_yet_reads_zero_not_negative(self):
        config.active_transfers[:] = [{"user": "e", "file": "x", "bytes_sent": 800, "size": 1000,
                                       "started_at": 990.0, "resume_offset": 800}]
        self.assertTrue(adminchat.status_lines(now=1000.0)[1].endswith(" 800 1000 0 x"))

    def test_a_stale_offset_larger_than_bytes_sent_is_not_a_negative_speed(self):
        config.active_transfers[:] = [{"user": "e", "file": "x", "bytes_sent": 100, "size": 1000,
                                       "started_at": 990.0, "resume_offset": 5000}]
        self.assertTrue(adminchat.status_lines(now=1000.0)[1].endswith(" 100 1000 0 x"))

    def test_a_transfer_that_has_just_started_reports_no_speed_yet(self):
        """Half a second of data is not a rate."""
        config.active_transfers[:] = [{"user": "e", "file": "x", "bytes_sent": 500, "size": 9, "started_at": 999.9}]
        self.assertTrue(adminchat.status_lines(now=1000.0)[1].endswith(" 500 9 0 x"))

    def test_a_row_without_size_or_clock_reads_zero(self):
        """Rows appended before start_dcc_send() stamps them."""
        config.active_transfers[:] = [{"user": "e", "file": "x", "bytes_sent": 0}]
        self.assertEqual(adminchat.status_lines()[1], "DCCORE SLOT e 0 0 0 x")

    def test_queue_lines_in_order_with_the_frozen_countdown(self):
        config.dcc_queue.update({"helen": [1, 2, 3, 4], "Ivan": [1]})
        config.frozen_queues["ivan"] = 950.0

        lines = adminchat.status_lines(now=1000.0)

        self.assertEqual(lines[1:], ["DCCORE QUEUE 1 helen 4 0", "DCCORE QUEUE 2 Ivan 1 250"])

    def test_the_queue_is_capped_at_the_head(self):
        for i in range(30):
            config.dcc_queue[f"user{i:02d}"] = [1]
        lines = adminchat.status_lines()
        self.assertEqual(len([l for l in lines if l.startswith("DCCORE QUEUE ")]), adminchat.QUEUE_LINES_MAX)

    def test_the_position_is_the_order_the_users_are_served_not_the_alphabet(self):
        """dcc.check_queue_and_send() walks dcc_queue in insertion order - the
        first to ask is the first served - so that is what <pos> must count.
        Sorted by nick, zed (who asked first) was shown as 2 (#612). helen and
        Ivan, which the test above uses, are the same in both orders and so
        could not tell them apart."""
        config.dcc_queue.update({"zed": [1, 2], "Alice": [1]})

        lines = adminchat.status_lines(now=1000.0)

        self.assertEqual(lines[1:], ["DCCORE QUEUE 1 zed 2 0", "DCCORE QUEUE 2 Alice 1 0"])

    def test_the_cap_keeps_the_head_of_the_queue_not_the_first_of_the_alphabet(self):
        """With more than QUEUE_LINES_MAX waiting, the rows are the ones next
        in line. Sorted by nick, the user at the head fell off the burst
        whenever their nick sorted late (#612)."""
        nicks = [f"nick_{chr(c)}" for c in range(ord("z"), ord("a") - 1, -1)]  # z first
        for nick in nicks:
            config.dcc_queue[nick] = [1]

        rows = [l.split() for l in adminchat.status_lines() if l.startswith("DCCORE QUEUE ")]

        self.assertEqual([r[3] for r in rows], nicks[:adminchat.QUEUE_LINES_MAX])
        self.assertEqual(rows[0][2:4], ["1", "nick_z"])

    def test_an_empty_queue_row_is_not_listed(self):
        config.dcc_queue["gone"] = []
        self.assertEqual(adminchat.status_lines()[0].split()[4:6], ["0", "0"])

    def test_figures_that_fail_to_load_read_zero_not_a_crash(self):
        def boom():
            raise RuntimeError("no stats")
        db.load_advanced_stats_rolled = boom
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            lines = adminchat.status_lines(now=1000.0)
        self.assertEqual(lines[0], "DCCORE STATUS 0 3 0 0 0 0 0 0 900 4 7")


class WhenTheBurstIsSent(DCCoreTestCase):
    def session(self, structured=True):
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = True
        s.structured = structured
        return s

    def statuses(self, s):
        return [l for l in s._outbox if l.startswith("DCCORE STATUS")]

    def test_hello_sends_a_first_burst(self):
        s = self.session(structured=False)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_command(s, "hello dccore.mrc 1.0")
        self.assertEqual(len(self.statuses(s)), 1)

    def test_an_event_that_moves_a_slot_or_the_queue_asks_for_one(self):
        """Asks - the writer sends it. Computing it here, on the emitting
        thread, deadlocked the bot (see the class below)."""
        for kind in ("SENDING", "SENT", "FAIL", "QUEUED", "RESUMED"):
            with self.subTest(kind=kind):
                s = self.session()
                s.event_sink(kind, {"nick": "d", "name": "x"}, "t")
                self.assertTrue(s._status_due)
                self.assertEqual(self.statuses(s), [], "nothing computed on this thread")

    def test_a_search_or_request_does_not(self):
        for kind in ("SEARCH", "REQUEST"):
            with self.subTest(kind=kind):
                s = self.session()
                s.event_sink(kind, {"nick": "d", "name": "x", "term": "t"}, "t")
                self.assertFalse(s._status_due)
                self.assertEqual(self.statuses(s), [])

    def test_the_writer_sends_the_burst_an_event_asked_for(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        s = adminchat.Session(a, "127.0.0.1", "SysOp", "h")
        s.authenticated = True; s.structured = True
        s._status_sent_at = time.time()          # the timer is not due
        s.start_writer()
        s.event_sink("SENT", {"nick": "d", "name": "x"}, "t")
        b.settimeout(3.0)
        buffer = b""
        deadline = time.time() + 3.0
        while time.time() < deadline and b"DCCORE STATUS " not in buffer:
            try:
                buffer += b.recv(65536)
            except socket.timeout:
                break
        s.close(None)
        self.assertIn(b"DCCORE STATUS ", buffer)
        self.assertLess(buffer.index(b"DCCORE SENT "), buffer.index(b"DCCORE STATUS "), "the event, then its burst")

    def test_a_plain_session_never_gets_one(self):
        s = self.session(structured=False)
        s.send_status()
        s.event_sink("SENT", {"nick": "d", "name": "x"}, "t")
        self.assertEqual(self.statuses(s), [])


class TheBurstNeverRunsOnTheEmittingThread(DCCoreTestCase):
    """2026-09-19, the first night with the mIRC script connected: the bot
    froze at a SENDING event and dropped off the network 24 minutes later.

    stats_mgr.live_speed() takes dcc.queue_lock, a plain Lock; status_lines()
    calls it; and SENDING is emitted from inside `with queue_lock:` in
    dcc.check_queue_and_send(). Computing the burst on the emitting thread
    was that thread taking a lock it already held. This is that thread."""

    def test_an_event_under_queue_lock_returns_at_once(self):
        import dcc
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = True; s.structured = True
        returned = threading.Event()

        def emit_under_the_lock():
            with dcc.queue_lock:
                s.event_sink("SENDING", {"nick": "d", "slot": 1, "slots": 3, "bytes": 1, "name": "x"}, "t")
            returned.set()

        thread = threading.Thread(target=emit_under_the_lock, daemon=True)
        thread.start()
        self.assertTrue(returned.wait(3.0), "event_sink deadlocked on queue_lock")
        self.assertTrue(s._status_due)

    def test_status_lines_does_take_queue_lock_which_is_why(self):
        """The hazard is real, not hypothetical: pinned so that a future
        status_lines() that drops live_speed() does not quietly make the
        test above meaningless."""
        with io.open(os.path.join(REPO_ROOT, "stats_mgr.py"), encoding="utf-8") as handle:
            self.assertIn("with dcc.queue_lock:", handle.read())
        with io.open(os.path.join(REPO_ROOT, "adminchat.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source.split("def status_lines(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("stats_mgr.live_speed()", body)
        sink = source.split("    def event_sink(", 1)[1].split("\n    def ", 1)[0]
        self.assertNotIn("self.send_status()", sink)
        self.assertIn("self.request_status()", sink)

    def test_the_timer_fills_silence_only(self):
        """A client that is behind is receiving lines already; a burst on
        top of a backlog would only push more of them off the outbox. So
        the writer drains everything queued before the timer speaks."""
        self.addCleanup(setattr, adminchat, "STATUS_INTERVAL", adminchat.STATUS_INTERVAL)
        adminchat.STATUS_INTERVAL = 0.0
        a, b = socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        s = adminchat.Session(a, "127.0.0.1", "SysOp", "h")
        s.authenticated = True; s.structured = True
        for i in range(adminchat.OUTBOX_MAX):
            s.send(f"DCCORE LOG INFO line {i}")
        s.start_writer()
        b.settimeout(3.0)
        buffer = b""
        deadline = time.time() + 3.0
        while time.time() < deadline and b"DCCORE STATUS " not in buffer:
            try:
                buffer += b.recv(65536)
            except socket.timeout:
                break
        s.close(None)
        head = buffer.split(b"DCCORE STATUS ", 1)[0]
        self.assertEqual(head.count(b"DCCORE LOG INFO line "), adminchat.OUTBOX_MAX)
        self.assertEqual(s.dropped, 0)

    def test_the_timer_sends_one_every_interval(self):
        """On the writer's own thread: with the interval shrunk, a structured
        session over a socketpair receives bursts without any event."""
        self.addCleanup(setattr, adminchat, "STATUS_INTERVAL", adminchat.STATUS_INTERVAL)
        adminchat.STATUS_INTERVAL = 0.2
        a, b = socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        s = adminchat.Session(a, "127.0.0.1", "SysOp", "h")
        s.authenticated = True; s.structured = True
        s.start_writer()
        b.settimeout(3.0)
        buffer = b""
        deadline = time.time() + 3.0
        while time.time() < deadline and buffer.count(b"DCCORE STATUS ") < 3:
            try:
                buffer += b.recv(65536)
            except socket.timeout:
                break
        s.close(None)
        self.assertGreaterEqual(buffer.count(b"DCCORE STATUS "), 3)


class TheHeartbeatDoesNotWaitOnQueueLock(DCCoreTestCase):
    """#614: the burst is also the heartbeat. The writer computed it inline,
    so a queue_lock (or a stats DB) held for the script's 90 seconds parked
    the writer with nothing at all going out - feed, LOG or STATUS - and
    the script called the link dead, reconnected, and the next writer
    parked at the same point: a login every ~100 s while the bot was fine.
    Now the figures get STATUS_WAIT on a helper; past that `DCCORE PING`
    stands in and the writer goes on draining."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, adminchat, "STATUS_WAIT", adminchat.STATUS_WAIT)
        adminchat.STATUS_WAIT = 0.2
        # live_speed() takes dcc.queue_lock only when its sample is stale,
        # and that timestamp is global state another test's leftover writer
        # thread can refresh underneath this one (it did, on a macOS
        # runner, and the helper sailed through). So the figure that blocks
        # is stubbed to take the lock unconditionally: while the test holds
        # queue_lock the helper cannot finish, and that is the property.
        import dcc
        import stats_mgr

        def speed_behind_the_lock(now=None):
            with dcc.queue_lock:
                return 0

        self.addCleanup(setattr, stats_mgr, "live_speed", stats_mgr.live_speed)
        stats_mgr.live_speed = speed_behind_the_lock

    def hold_queue_lock(self):
        import dcc
        self.assertTrue(dcc.queue_lock.acquire(timeout=3.0), "queue_lock is already held")
        self.addCleanup(dcc.queue_lock.release)

    def recv_until(self, sock, needle, seconds=3.0):
        sock.settimeout(seconds)
        buffer = b""
        deadline = time.time() + seconds
        while time.time() < deadline and needle not in buffer:
            try:
                buffer += sock.recv(65536)
            except socket.timeout:
                break
        return buffer

    def send_status_or_fail(self, s):
        """send_status on its own thread: the old code parked on the lock,
        and a test that parks with it hangs the run instead of failing."""
        returned = threading.Event()
        threading.Thread(target=lambda: (s.send_status(), returned.set()), daemon=True).start()
        self.assertTrue(returned.wait(3.0), "send_status parked on queue_lock")

    def test_a_ping_stands_in_for_a_burst_the_lock_holds_up(self):
        self.hold_queue_lock()
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = True; s.structured = True
        self.send_status_or_fail(s)
        self.assertEqual(list(s._outbox), ["DCCORE PING"])
        self.assertIsNotNone(s._status_job, "the figures are still being read")

    def test_the_feed_keeps_flowing_while_the_lock_is_held(self):
        """The audit's probe: the writer running, the lock taken, the timer
        due - and a feed line and a LOG line emitted behind it. They reached
        nobody; the outbox just grew."""
        self.hold_queue_lock()
        a, b = socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        s = adminchat.Session(a, "127.0.0.1", "SysOp", "h")
        s.authenticated = True; s.structured = True
        s._status_sent_at = 0                      # the timer is due at once
        s.start_writer()
        buffer = self.recv_until(b, b"DCCORE PING\n")
        self.assertIn(b"DCCORE PING\n", buffer, "nothing was heard: the writer is parked")
        s.event_sink("SENDING", {"nick": "dana", "slot": 1, "slots": 3, "bytes": 1, "name": "x"}, "t")
        s.debug_sink("something happened", "INFO")
        buffer += self.recv_until(b, b"DCCORE LOG INFO something happened\n")
        s.close(None)
        self.assertIn(b"DCCORE SENDING dana", buffer)
        self.assertIn(b"DCCORE LOG INFO something happened\n", buffer)
        self.assertNotIn(b"DCCORE STATUS ", buffer, "no figures could be read while the lock was held")

    def test_the_burst_follows_once_the_lock_frees(self):
        """The helper is not abandoned and not doubled: the pass that finds
        it finished in time sends its lines, and a fresh one is started
        only when none is running."""
        import dcc
        self.assertTrue(dcc.queue_lock.acquire(timeout=3.0), "queue_lock is already held")
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = True; s.structured = True
        try:
            self.send_status_or_fail(s)
            helper = s._status_job[0]
            self.send_status_or_fail(s)
            self.assertIs(s._status_job[0], helper, "a second helper was started behind the first")
            self.assertEqual(list(s._outbox), ["DCCORE PING", "DCCORE PING"])
        finally:
            dcc.queue_lock.release()
        self.send_status_or_fail(s)
        self.assertIsNone(s._status_job)
        self.assertEqual(sum(l.startswith("DCCORE STATUS ") for l in s._outbox), 1,
                         "one burst: the helper that was waited for, and no other")

    def test_the_script_swallows_the_ping(self):
        """Any line resets the script's 90 s timer, so PING needs no handling
        beyond not being echoed as an unknown type."""
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("if (%type == PING) { return }", script)
        timer = script.index(".timerdccoreHB 1 90 dccore.dead")
        self.assertLess(timer, script.index("dccore.line $1-"), "the timer is reset before the line is read")


class Pairing(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = self.make_tree().root
        self._real_path = db.ADMIN_TOKENS_FILE
        db.ADMIN_TOKENS_FILE = os.path.join(self.tmp, "adminchat_tokens.json")
        self.addCleanup(setattr, db, "ADMIN_TOKENS_FILE", self._real_path)
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash("pw", iterations=1000))
        adminchat.WRONG_PASSWORD_DELAY, real = 0.0, adminchat.WRONG_PASSWORD_DELAY
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", real)

    def session(self, authenticated=True, structured=False):
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = authenticated
        s.structured = structured
        return s

    def pair(self, name="dccore.mrc", structured=False):
        s = self.session(structured=structured)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_command(s, f"pair {name} 1.0")
        lines = list(s._outbox)
        if structured:
            token = [l for l in lines if l.startswith("DCCORE TOKEN ")][0].split()[3]
        else:
            token = [l.strip() for l in lines if l.startswith("  ")][0]
        return token, lines

    def test_pair_mints_a_token_and_keeps_only_its_hash(self):
        token, lines = self.pair()

        self.assertGreaterEqual(len(token), 32)
        stored = db.load_admin_tokens()
        self.assertEqual(list(stored), ["dccore.mrc"])
        self.assertNotIn(token, io.open(db.ADMIN_TOKENS_FILE, encoding="utf-8").read())
        self.assertTrue(stored["dccore.mrc"]["hash"].startswith("pbkdf2"))
        self.assertIn("shown once", " ".join(lines))

    def test_in_structured_mode_the_token_is_a_dccore_line(self):
        token, lines = self.pair(structured=True)
        self.assertIn(f"DCCORE TOKEN dccore.mrc {token}", lines)

    def test_the_token_logs_in(self):
        token, _ = self.pair()
        s = self.session(authenticated=False)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(s, token)
        self.assertTrue(s.authenticated)

    def test_the_password_still_logs_in(self):
        self.pair()
        s = self.session(authenticated=False)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(s, "pw")
        self.assertTrue(s.authenticated)

    def test_a_wrong_token_is_a_wrong_password(self):
        self.pair()
        s = self.session(authenticated=False)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(s, "not-a-token")
        self.assertFalse(s.authenticated)
        self.assertEqual(s.attempts, 1)

    def test_a_token_does_not_open_the_dashboard(self):
        """The dashboard verifies against ADMIN_PASSWORD_HASH and never
        reads the token store - the whole reason tokens exist."""
        token, _ = self.pair()
        self.assertFalse(adminchat.verify_password(config.ADMIN_PASSWORD_HASH, token))
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("_token_matches", source)
        self.assertNotIn("load_admin_tokens", source)

    def test_pairing_again_rotates_the_token(self):
        first, _ = self.pair()
        second, _ = self.pair()
        s = self.session(authenticated=False)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(s, first)
        self.assertFalse(s.authenticated, "the replaced token still works")
        self.assertEqual(len(db.load_admin_tokens()), 1)

    def test_unpair_lists_and_revokes(self):
        token, _ = self.pair()
        s = self.session()
        adminchat.handle_command(s, "unpair")
        self.assertIn("dccore.mrc", " ".join(s._outbox))

        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_command(s, "unpair dccore.mrc")
        self.assertEqual(db.load_admin_tokens(), {})
        login = self.session(authenticated=False)
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(login, token)
        self.assertFalse(login.authenticated)

    def test_unpair_of_a_stranger_says_so(self):
        s = self.session()
        adminchat.handle_command(s, "unpair nobody")
        self.assertIn("No paired client", " ".join(s._outbox))

    def test_pair_and_unpair_are_console_commands_only(self):
        """Reachable through COMMANDS - so authenticated only - and not
        through the channel command set."""
        self.assertIn("pair", adminchat.COMMANDS)
        self.assertIn("unpair", adminchat.COMMANDS)
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            self.assertNotIn("!pair", handle.read())


@unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
class OverARealChat(unittest.TestCase):
    """Pair with the password, reconnect with the token, get a burst."""

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        config.ADMIN_HOSTMASKS = ["*!*@SysOp.users.undernet.org"]
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash(PASSWORD, iterations=1000)
        config.NICKNAME = "MusicBot"
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp(prefix="dccore-tokens-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._real_path = db.ADMIN_TOKENS_FILE
        db.ADMIN_TOKENS_FILE = os.path.join(self.tmp, "t.json")
        self.addCleanup(setattr, db, "ADMIN_TOKENS_FILE", self._real_path)

    def dial(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0)); listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        adminchat.handle_dcc_chat(None, ADMIN_LINE, "SysOp", f"DCC CHAT chat 2130706433 {port}")
        listener.settimeout(5.0)
        client, _ = listener.accept()
        client.settimeout(5.0)
        self.addCleanup(client.close)
        return client

    def read_until(self, client, needle, timeout=5.0):
        buffer = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            if needle in buffer:
                return buffer
        return buffer

    def test_pair_then_log_in_with_the_token(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            first = self.dial()
            self.read_until(first, "Enter Your Password:")
            first.sendall((PASSWORD + "\n").encode())
            self.read_until(first, "For help type")
            first.sendall(b"hello dccore.mrc 1.0\npair dccore.mrc 1.0\n")
            text = self.read_until(first, "DCCORE TOKEN ")
            token = [l for l in text.splitlines() if l.startswith("DCCORE TOKEN ")][0].split()[3]
            self.assertIn("DCCORE STATUS ", text, "hello brought a first burst")
            first.close()

            second = self.dial()
            self.read_until(second, "Enter Your Password:")
            second.sendall((token + "\n").encode())
            text = self.read_until(second, "For help type")
        self.assertIn("Entering DCC Chat Admin Interface", text)


if __name__ == "__main__":
    unittest.main()


class TheResumeIsRecordedOnTheRow(unittest.TestCase):
    """The speed above needs to know where this connection started (#746)."""

    def test_a_resumed_send_stores_its_offset_beside_bytes_sent(self):
        import io
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("tx['bytes_sent'] = resume_offset")
        self.assertIn("tx['resume_offset'] = resume_offset", source[start:start + 400])


class TheBotsOwnStartAndCounts(DCCoreTestCase):
    """The panel's Since box counts from the BOT's start (#754), not the window's."""

    def setUp(self):
        super().setUp()
        import stats_mgr
        import runtime
        real = stats_mgr.get_uptime_seconds
        stats_mgr.get_uptime_seconds = lambda: 3600
        self.addCleanup(setattr, stats_mgr, "get_uptime_seconds", real)
        self.saved = dict(runtime.feed_counts)
        runtime.feed_counts.update({"FAIL": 0, "SEARCH": 0})
        self.addCleanup(lambda: runtime.feed_counts.update(self.saved))
        import announce
        self.announce = announce
        real_debug = announce.send_debug
        announce.send_debug = lambda *a, **k: None
        self.addCleanup(setattr, announce, "send_debug", real_debug)

    def status(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            return adminchat.status_lines(now=10000.0)[0].split()

    def fail(self, text="x"):
        self.announce.feed_event("FAIL", text, nick="a", channel="#c", acked_bytes=0, total_bytes=1, name="f", reason="r")

    def test_the_start_is_now_minus_the_uptime(self):
        self.assertEqual(self.status()[9], "6400")

    def test_a_failure_and_a_search_are_counted_where_they_pass(self):
        self.fail("x")
        self.fail("y")
        self.announce.feed_event("SEARCH", "z", nick="a", channel="#c", results=1, term="t")
        self.assertEqual(self.status()[10:], ["2", "1"])

    def test_the_other_kinds_are_not_counted(self):
        self.announce.feed_event("SENT", "s", nick="a", channel="#c", bytes=1, seconds=1.0, bytes_per_s=1, name="f")
        self.announce.feed_event("REQUEST", "r", nick="a", channel="#c", kind="file", name="f")
        self.assertEqual(self.status()[10:], ["0", "0"])

    def test_the_counts_live_in_runtime_so_a_rehash_does_not_reset_them(self):
        import importlib
        import runtime
        self.fail("x")
        importlib.reload(self.announce)
        self.assertEqual(runtime.feed_counts["FAIL"], 1)
        self.announce.send_debug = lambda *a, **k: None
        self.fail("y")
        self.assertEqual(runtime.feed_counts["FAIL"], 2)

    def test_a_console_that_refuses_the_kind_still_lets_it_be_counted(self):
        self.set_config(CONSOLE_SHOW_SEARCHES=False)
        self.announce.feed_event("SEARCH", "z", nick="a", channel="#c", results=1, term="t")
        self.assertEqual(self.status()[10:], ["0", "1"])

    def test_the_doc_table_names_the_three_fields(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            self.assertIn("<started> <failed> <searches>", handle.read())


class ThePanelReadsThem(unittest.TestCase):

    def panel(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
            text = handle.read().replace("\r\n", "\n")
        return text.split("alias dccore.panel {", 1)[1].split("\n}", 1)[0]

    def test_since_is_the_bots_start_when_it_says_and_the_window_otherwise(self):
        self.assertIn("var %since = $iif($dccore.st(st.started) > 0,$dccore.st(st.started),$dccore.st(opened))", self.panel())

    def test_a_start_more_than_a_day_ago_shows_the_day(self):
        self.assertIn("ddd HH:nn,HH:nn", self.panel())

    def test_the_counts_are_the_bots_and_fall_back_to_the_windows(self):
        panel = self.panel()
        self.assertIn("$iif($dccore.st(st.failed) != $null,$dccore.st(st.failed),$dccore.st(failed))", panel)
        self.assertIn("$iif($dccore.st(st.searches) != $null,$dccore.st(st.searches),$dccore.st(searches))", panel)
