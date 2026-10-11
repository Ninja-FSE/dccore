"""Console logins from one address got more guesses than the limit.

The DCC console blocks an address for BAD_IP_BLOCK_SECONDS after
MAX_PASSWORD_ATTEMPTS wrong passwords. The block was checked once, when the
chat was opened, and an attempt was counted only after its password had been
verified (PBKDF2, deliberately slow). So a session opened before the block
went on being verified after it, and parallel sessions from one address each
passed the check before any of them had been counted - more guesses between
them than the limit allows. The dashboard's login had the same shape.

An attempt is now reserved before it is verified: the block is checked and
the attempt counted in one step under runtime.admin_bad_ip_lock, and a right
password settles it by clearing the address.

The real _check_password(), with verify_password() held inside so every
login is in flight at once.
"""

import contextlib
import io
import os
import socket
import sys
import threading

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

ADDRESS = "203.0.113.9"


def wait_for(condition, timeout=10.0):
    done = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if condition():
            return True
        done.wait(0.01)
    return condition()


class LoginCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash("right", iterations=1000))
        real_delay = adminchat.WRONG_PASSWORD_DELAY
        adminchat.WRONG_PASSWORD_DELAY = 0.0
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", real_delay)
        self.out = io.StringIO()

    def session(self):
        s = adminchat.Session(socket.socket(), ADDRESS, "alfa", "alfa.host.example")
        self.addCleanup(s.close, None)
        return s

    def attempt(self, session, password):
        with contextlib.redirect_stdout(self.out):
            adminchat._check_password(session, password)


class TheLockLivesInRuntime(LoginCase):

    def test_the_pool_is_guarded_by_runtimes_lock(self):
        self.assertIs(adminchat._bad_lock, runtime.admin_bad_ip_lock)


class ParallelLogins(LoginCase):

    def all_at_once(self, attempts):
        """Run every callable in `attempts` on a thread of its own, with
        verify_password() held until each is being verified or has been
        turned away; how many were being verified at once.

        The bot's log is caught once around the lot: redirect_stdout() on
        each thread would restore stdout in the wrong order."""
        inside = []
        finished = []
        release = threading.Event()
        real_verify = adminchat.verify_password

        def held_verify(stored, supplied):
            inside.append(supplied)
            release.wait(10)
            return real_verify(stored, supplied)
        adminchat.verify_password = held_verify
        self.addCleanup(setattr, adminchat, "verify_password", real_verify)

        def run(attempt):
            attempt()
            finished.append(attempt)
        threads = [threading.Thread(target=run, args=(attempt,), daemon=True) for attempt in attempts]
        with contextlib.redirect_stdout(self.out):
            for thread in threads:
                thread.start()
            arrived = wait_for(lambda: len(inside) + len(finished) == len(attempts))
            verified_together = len(inside)
            release.set()
            for thread in threads:
                thread.join(10)
        self.assertTrue(arrived, "the attempts never all arrived")
        return verified_together

    def test_logins_in_flight_together_get_no_more_than_the_limit(self):
        tries = 2 * adminchat.MAX_PASSWORD_ATTEMPTS
        sessions = [self.session() for _ in range(tries)]

        verified_together = self.all_at_once(
            [lambda s=s: adminchat._check_password(s, "guess") for s in sessions])

        self.assertEqual(verified_together, adminchat.MAX_PASSWORD_ATTEMPTS,
                         f"{verified_together} passwords from one address were being "
                         f"checked at once; the limit is {adminchat.MAX_PASSWORD_ATTEMPTS}")
        self.assertTrue(adminchat.is_bad_ip(ADDRESS))
        refused = [s for s in sessions if s.closed and s.attempts == 0]
        self.assertEqual(len(refused), tries - adminchat.MAX_PASSWORD_ATTEMPTS)
        self.assertIn("refused: the address is blocked", self.out.getvalue())

    def test_unlocks_in_flight_together_get_no_more_than_the_limit(self):
        """`unlock <password>` on a token session (#1264) checks the same
        password, so it shares the same limit: token sessions from one
        address sending unlocks side by side get no more guesses."""
        import console_settings
        tries = 2 * adminchat.MAX_PASSWORD_ATTEMPTS
        sessions = []
        for _ in range(tries):
            s = self.session()
            s.authenticated = True
            s.unlocked = False
            s.paired_as = "dccore.mrc-0a1b2c3d"
            sessions.append(s)

        verified_together = self.all_at_once(
            [lambda s=s: console_settings.cmd_unlock(s, "guess") for s in sessions])

        self.assertEqual(verified_together, adminchat.MAX_PASSWORD_ATTEMPTS,
                         f"{verified_together} unlock passwords from one address were being "
                         f"checked at once; the limit is {adminchat.MAX_PASSWORD_ATTEMPTS}")
        self.assertTrue(adminchat.is_bad_ip(ADDRESS))
        self.assertFalse(any(s.unlocked for s in sessions))
        refused = [s for s in sessions if s.closed and not s.unlock_failures]
        self.assertEqual(len(refused), tries - adminchat.MAX_PASSWORD_ATTEMPTS)

    def test_a_wrong_encoded_unlock_is_counted_once(self):
        """The settings window sends a spaced password encoded (#1281), and
        unlock_password_matches() verifies it as sent and then decoded - under
        the one reservation, so it costs the address one attempt."""
        import console_settings
        s = self.session()
        s.authenticated = True
        s.unlocked = False
        with contextlib.redirect_stdout(self.out):
            console_settings.cmd_unlock(s, "%20guess  %20")
        self.assertFalse(s.unlocked)
        self.assertEqual(s.unlock_failures, 1)
        self.assertEqual(adminchat._bad_ips[ADDRESS][0], 1)

    def test_a_right_encoded_unlock_clears_the_address(self):
        import console_settings
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(" right ", iterations=1000))
        s = self.session()
        s.authenticated = True
        s.unlocked = False
        with contextlib.redirect_stdout(self.out):
            console_settings.cmd_unlock(s, "guess")
            console_settings.cmd_unlock(s, "%20right%20")
        self.assertTrue(s.unlocked)
        self.assertNotIn(ADDRESS, adminchat._bad_ips)

    def test_a_blocked_address_cannot_unlock_even_with_the_password(self):
        import console_settings
        first = self.session()
        with contextlib.redirect_stdout(self.out):
            for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
                adminchat._check_password(first, "guess")
        token_session = self.session()
        token_session.authenticated = True
        token_session.unlocked = False

        with contextlib.redirect_stdout(self.out):
            console_settings.cmd_unlock(token_session, "right")

        self.assertFalse(token_session.unlocked)
        self.assertTrue(token_session.closed)

    def test_each_wrong_unlock_is_counted_once(self):
        import console_settings
        s = self.session()
        s.authenticated = True
        s.unlocked = False
        with contextlib.redirect_stdout(self.out):
            console_settings.cmd_unlock(s, "guess")
        self.assertEqual(adminchat._bad_ips[ADDRESS][0], 1)
        with contextlib.redirect_stdout(self.out):
            console_settings.cmd_unlock(s, "right")
        self.assertTrue(s.unlocked)
        self.assertNotIn(ADDRESS, adminchat._bad_ips)

    def test_a_session_opened_before_the_block_is_not_verified_after_it(self):
        """The same hole one at a time: the chat was let in while the address
        was clean, and the address has been blocked since."""
        first, second = self.session(), self.session()
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.attempt(first, "guess")
        self.assertTrue(adminchat.is_bad_ip(ADDRESS))

        checked = []
        real_verify = adminchat.verify_password
        adminchat.verify_password = lambda stored, supplied: checked.append(supplied) or False
        self.addCleanup(setattr, adminchat, "verify_password", real_verify)

        self.attempt(second, "right")

        self.assertEqual(checked, [], "a blocked address still had its password checked")
        self.assertTrue(second.closed)
        self.assertFalse(second.authenticated)


class TheReservationIsSettled(LoginCase):

    def test_a_right_password_at_the_last_attempt_logs_in_and_clears_the_address(self):
        s = self.session()
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS - 1):
            self.attempt(s, "guess")
        self.attempt(s, "right")

        self.assertTrue(s.authenticated)
        self.assertFalse(adminchat.is_bad_ip(ADDRESS))
        self.assertNotIn(ADDRESS, adminchat._bad_ips)

    def test_each_wrong_password_is_counted_once(self):
        s = self.session()
        self.attempt(s, "guess")
        self.assertEqual(adminchat._bad_ips[ADDRESS][0], 1)
        self.attempt(s, "guess")
        self.assertEqual(adminchat._bad_ips[ADDRESS][0], 2)
        self.assertFalse(adminchat.is_bad_ip(ADDRESS))

    def test_an_empty_line_is_not_an_attempt(self):
        self.attempt(self.session(), "")
        self.assertNotIn(ADDRESS, adminchat._bad_ips)

    def test_a_spaced_password_line_is_one_attempt(self):
        """`DCCORE PASSWORD <value>` (#1273) is verified twice - as sent, and
        decoded - and still costs the address one attempt, not two."""
        s = self.session()
        self.attempt(s, "DCCORE PASSWORD %20guess")
        self.assertEqual(adminchat._bad_ips[ADDRESS][0], 1)
        self.assertEqual(s.attempts, 1)

    def test_a_spaced_password_line_logs_in_and_clears_the_address(self):
        s = self.session()
        self.attempt(s, "guess")
        self.attempt(s, "DCCORE PASSWORD right")
        self.assertTrue(s.authenticated)
        self.assertNotIn(ADDRESS, adminchat._bad_ips)


if __name__ == "__main__":
    import unittest
    unittest.main()
