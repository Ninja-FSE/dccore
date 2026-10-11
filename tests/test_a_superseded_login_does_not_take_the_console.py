"""#1273: a login replaced while its password was being checked still logged in.

Only one connection may sit at the password prompt: a newer one closes it
("Superseded by a newer connection."). But checking a password takes a moment
- PBKDF2, and once more for each paired token - and the replaced connection's
own thread was still inside that check. When it finished it promoted the
CLOSED session: the operator's live console was closed as "taken over", the
closed session became the live one until its thread noticed and forgot it, so
the operator was left with no console at all, and the sinks it registered were
never removed, because its close() had already run.

A session that is no longer the pending one, or is closed, is not promoted
now, and sinks registered for a session that closes in between are removed.
"""

import contextlib
import io
import socket
import threading
import time
import unittest

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase  # noqa: E402

import adminchat  # noqa: E402
import announce  # noqa: E402
import platform_compat  # noqa: E402

PASSWORD = "correct horse"


class ASupersededLogin(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(PASSWORD, iterations=1000))
        self.addCleanup(adminchat.reset_state_for_tests)
        adminchat.reset_state_for_tests()
        # The operator's console, logged in and live.
        self.live, self.live_far = self.session("192.0.2.10")
        self.live.authenticated = True
        adminchat._session = self.live
        # A second connection at the password prompt, as _serve() leaves it.
        self.late, self.late_far = self.session("192.0.2.20")
        adminchat._pending = self.late
        self.real_verify = adminchat.verify_password
        self.addCleanup(setattr, adminchat, "verify_password", self.real_verify)

    def session(self, peer_ip):
        near, far = socket.socketpair()
        self.addCleanup(far.close)
        session = adminchat.Session(near, peer_ip, "alfa", "alfa.users.example")
        self.addCleanup(session.close, None)
        return session, far

    def verify_while(self, meanwhile):
        """verify_password(), with `meanwhile` happening while it runs."""
        def verify(stored, supplied):
            nonlocal meanwhile
            if meanwhile is not None:
                happen, meanwhile = meanwhile, None
                happen()
            return self.real_verify(stored, supplied)
        adminchat.verify_password = verify

    def check(self, session, line):
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(session, line)

    def assert_the_live_console_is_untouched(self):
        self.assertFalse(self.live.closed, "the operator's live console must not be taken over")
        self.assertIs(adminchat._session, self.live)
        self.assertFalse(self.late.authenticated)
        self.assertNotIn(self.late.debug_sink, announce._debug_sinks)
        self.assertNotIn(self.late.event_sink, announce._event_sinks)

    def test_a_newer_connection_arriving_during_the_check_wins(self):
        newer_near, newer_far = socket.socketpair()
        self.addCleanup(newer_far.close)
        serving = []
        # _serve() turns on TCP keepalive. A Linux socket pair is AF_UNIX and
        # refuses TCP options (EOPNOTSUPP), so the thread died before it took
        # the prompt - red on the Ubuntu runners only; macOS has no
        # TCP_KEEPIDLE and Windows pairs over TCP. The bot's real connections
        # are TCP. What is tested here is the race, not keepalive.
        real_keepalive = platform_compat.apply_keepalive
        platform_compat.apply_keepalive = lambda sock, *a, **k: sock
        self.addCleanup(setattr, platform_compat, "apply_keepalive", real_keepalive)

        def a_newer_connection():
            # The real _serve(), on its own thread as the bot runs it: it
            # takes the prompt and closes the connection it replaces.
            thread = threading.Thread(target=adminchat._serve, daemon=True,
                                      args=(newer_near, "192.0.2.30", "alfa", "alfa.users.example", "test"))
            serving.append(thread)
            with contextlib.redirect_stdout(io.StringIO()):
                thread.start()
                deadline = time.monotonic() + 10
                while not self.late.closed and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertTrue(self.late.closed, "the newer connection never replaced the pending one")

        self.verify_while(a_newer_connection)
        self.check(self.late, PASSWORD)

        self.assert_the_live_console_is_untouched()
        newer = adminchat._pending
        self.assertIsNotNone(newer, "the newer connection must still be at the prompt")
        self.assertEqual(newer.peer_ip, "192.0.2.30")
        newer.close(announce_text=None)
        serving[0].join(10)
        self.assertFalse(serving[0].is_alive())

    def test_replaced_but_not_yet_closed_is_refused_too(self):
        # _serve() swaps the pending session under the lock and closes the
        # old one only after letting go: the check can end in between.
        newer, _far = self.session("192.0.2.30")
        self.verify_while(lambda: setattr(adminchat, "_pending", newer))
        self.check(self.late, PASSWORD)
        self.assert_the_live_console_is_untouched()
        self.assertIs(adminchat._pending, newer)

    def test_closed_during_the_check_is_refused(self):
        def closed():
            self.late.close(announce_text=None)
            adminchat._forget(self.late)
        self.verify_while(closed)
        self.check(self.late, PASSWORD)
        self.assert_the_live_console_is_untouched()

    def test_an_ordinary_login_still_takes_over(self):
        self.check(self.late, PASSWORD)
        self.assertTrue(self.late.authenticated)
        self.assertIs(adminchat._session, self.late)
        self.assertTrue(self.live.closed)
        self.assertIn(self.late.debug_sink, announce._debug_sinks)

    def test_sinks_for_a_session_closed_while_they_were_added_are_removed(self):
        adminchat._session = None
        adminchat._pending = None
        real_add = announce.add_debug_sink

        def add_then_close(sink):
            real_add(sink)
            self.late.close(announce_text=None)

        self.addCleanup(setattr, announce, "add_debug_sink", real_add)
        announce.add_debug_sink = add_then_close
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._promote(self.late)
        self.assertNotIn(self.late.debug_sink, announce._debug_sinks)
        self.assertNotIn(self.late.event_sink, announce._event_sinks,
                         "close() had already run, so nothing else would ever remove it")


if __name__ == "__main__":
    unittest.main()
