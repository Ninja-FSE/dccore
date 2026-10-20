"""A token login keeps the address's failed-password count.

The DCC console counts wrong passwords per address, at the login prompt and
in `unlock`, and blocks the address for BAD_IP_BLOCK_SECONDS once there are
MAX_PASSWORD_ATTEMPTS of them. Only the admin password clears that count
now. A login with a paired token, full or read-only, takes back the one
attempt reserve_password_attempt() counted for it and leaves the earlier
ones as they were - before, it cleared the address's record too, the same
as the password.

The real _check_password(), cmd_unlock() (through handle_command()),
reserve_password_attempt(), release_password_attempt() and clear_bad_ip();
only _promote() and the wrong-password delay are stood in for.
"""

import contextlib
import io
import os
import socket
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import db  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

ADDRESS = "198.51.100.23"
PASSWORD = "the right one"
FULL_TOKEN = "full-0f1e2d3c4b5a6978"
READ_ONLY_TOKEN = "look-8796a5b4c3d2e1f0"
LIMIT = adminchat.MAX_PASSWORD_ATTEMPTS


class CountCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(PASSWORD, iterations=1000))
        db.save_admin_tokens({
            "dccore.mrc-5e6f7a8b": {"hash": adminchat.make_password_hash(FULL_TOKEN, iterations=1000)},
            "stats": {"readonly_hash": adminchat.make_password_hash(READ_ONLY_TOKEN, iterations=1000)},
        })
        real_delay = adminchat.WRONG_PASSWORD_DELAY
        adminchat.WRONG_PASSWORD_DELAY = 0.0
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", real_delay)
        # _promote() swaps the one console session and attaches the log
        # sinks; what it answers is set per test.
        self.promoted = None
        real_promote = adminchat._promote
        adminchat._promote = lambda session: self.promoted
        self.addCleanup(setattr, adminchat, "_promote", real_promote)
        self.out = io.StringIO()

    def session(self):
        s = adminchat.Session(socket.socket(), ADDRESS, "alfa", "alfa.users.example")
        self.addCleanup(s.close, None)
        return s

    def login(self, supplied, session=None):
        """A new session (unless given) with `supplied` at the prompt."""
        session = session or self.session()
        with contextlib.redirect_stdout(self.out):
            adminchat._check_password(session, supplied)
        return session

    def unlock(self, session, password):
        with contextlib.redirect_stdout(self.out):
            adminchat.handle_command(session, "unlock " + password)

    def record(self):
        entry = adminchat._bad_ips.get(ADDRESS)
        return list(entry) if entry else None

    def blocked(self):
        return adminchat.is_bad_ip(ADDRESS)

    def wrong_at_the_prompt(self, times):
        session = self.session()
        for n in range(times):
            self.login(f"wrong {n}", session)
        return session


class TheCountAddsUpAcrossTokenLogins(CountCase):

    def test_token_logins_between_wrong_unlocks_reach_the_block(self):
        """Wrong unlocks spread over token logins still reach the block."""
        wrong = 0
        for _cycle in range(5 * LIMIT):
            session = self.login(FULL_TOKEN)
            if not session.authenticated:
                break
            for _ in range(2):
                self.unlock(session, f"wrong {wrong}")
                if session.unlocked:
                    self.fail("a wrong password unlocked the session")
                if session.closed:
                    break
                wrong += 1
        self.assertEqual(wrong, LIMIT, f"{wrong} wrong unlocks before the block; the limit is {LIMIT}")
        self.assertTrue(self.blocked())
        self.assertTrue(session.closed)
        self.assertFalse(session.authenticated)

    def test_wrong_passwords_then_a_token_reach_the_block(self):
        """Wrong passwords spread over token logins still reach the block."""
        wrong = 0
        refused = False
        for _cycle in range(5 * LIMIT):
            session = self.session()
            for _ in range(2):
                self.login(f"wrong {wrong}", session)
                if session.authenticated:
                    self.fail("a wrong password logged in")
                if session.closed:
                    refused = True
                    break
                wrong += 1
            if refused:
                break
            self.login(READ_ONLY_TOKEN, session)
            if not session.authenticated:
                refused = True
                break
        self.assertTrue(refused, "the address was never refused")
        self.assertEqual(wrong, LIMIT, f"{wrong} wrong passwords before the block; the limit is {LIMIT}")
        self.assertTrue(self.blocked())

    def test_the_same_with_the_full_token_at_the_prompt(self):
        self.wrong_at_the_prompt(2)
        self.assertTrue(self.login(FULL_TOKEN).authenticated)
        self.wrong_at_the_prompt(1)
        self.assertTrue(self.blocked())
        self.assertFalse(self.login(FULL_TOKEN).authenticated)


class ABlockedAddressIsRefusedWithAToken(CountCase):

    def block(self):
        self.wrong_at_the_prompt(LIMIT)
        self.assertTrue(self.blocked())

    def test_at_the_prompt(self):
        self.block()
        for token in (FULL_TOKEN, READ_ONLY_TOKEN):
            with self.subTest(token=token):
                session = self.login(token)
                self.assertFalse(session.authenticated)
                self.assertTrue(session.closed)
        self.assertTrue(self.blocked())

    def test_at_unlock_even_with_the_password(self):
        """A token session opened before the block: its unlock is refused
        and the session closed, the right password included."""
        session = self.login(FULL_TOKEN)
        self.assertTrue(session.authenticated)
        self.block()
        self.unlock(session, PASSWORD)
        self.assertFalse(session.unlocked)
        self.assertTrue(session.closed)
        self.assertTrue(self.blocked())

    def test_an_unlock_that_reaches_the_limit_blocks_the_next(self):
        self.wrong_at_the_prompt(LIMIT - 1)
        session = self.login(FULL_TOKEN)
        self.unlock(session, "wrong")
        self.assertTrue(self.blocked())
        self.assertFalse(session.closed, "one wrong unlock in this session: still open")
        self.unlock(session, PASSWORD)
        self.assertFalse(session.unlocked)
        self.assertTrue(session.closed)


class ThePasswordStillClearsIt(CountCase):

    def test_at_the_prompt(self):
        session = self.wrong_at_the_prompt(LIMIT - 1)
        self.login(PASSWORD, session)
        self.assertTrue(session.authenticated)
        self.assertTrue(session.unlocked)
        self.assertIsNone(self.record())

    def test_in_unlock(self):
        self.wrong_at_the_prompt(1)
        session = self.login(FULL_TOKEN)
        self.unlock(session, "wrong")
        self.assertEqual(self.record()[0], 2)
        self.unlock(session, PASSWORD)
        self.assertTrue(session.unlocked)
        self.assertIsNone(self.record())

    def test_a_password_login_is_not_counted(self):
        self.login(PASSWORD)
        self.assertIsNone(self.record())


class ATokenLeavesTheCountAsItWas(CountCase):

    def test_no_record_stays_no_record(self):
        for token in (FULL_TOKEN, READ_ONLY_TOKEN):
            with self.subTest(token=token):
                self.assertTrue(self.login(token).authenticated)
                self.assertIsNone(self.record())

    def test_a_count_is_neither_cleared_nor_raised(self):
        for failures in range(1, LIMIT):
            for token in (FULL_TOKEN, READ_ONLY_TOKEN):
                with self.subTest(failures=failures, token=token):
                    adminchat.reset_state_for_tests()
                    self.wrong_at_the_prompt(failures)
                    before = self.record()
                    session = self.login(token)
                    self.assertTrue(session.authenticated)
                    self.assertEqual(session.read_only, token == READ_ONLY_TOKEN)
                    self.assertFalse(session.unlocked)
                    self.assertEqual(self.record(), before, "count, block and last failure as they were")
                    self.assertFalse(self.blocked())

    def test_on_the_same_chat_as_the_wrong_passwords(self):
        session = self.wrong_at_the_prompt(LIMIT - 1)
        before = self.record()
        self.login(FULL_TOKEN, session)
        self.assertTrue(session.authenticated)
        self.assertEqual(self.record(), before)

    def test_old_failures_still_expire(self):
        """The token does not make the earlier failures recent again."""
        self.wrong_at_the_prompt(LIMIT - 1)
        adminchat._bad_ips[ADDRESS][2] = time.time() - adminchat.BAD_IP_BLOCK_SECONDS / 2
        before = self.record()
        self.login(FULL_TOKEN)
        self.assertEqual(self.record(), before)
        self.assertEqual(adminchat.forget_stale_failures(
            adminchat._bad_ips, time.time() + adminchat.BAD_IP_BLOCK_SECONDS / 2 + 1), 1)
        self.assertIsNone(self.record())


class ARefusedTokenLoginLeavesItToo(CountCase):

    def test_a_read_only_token_refused_while_the_console_is_in_use(self):
        self.promoted = adminchat._IN_USE
        self.wrong_at_the_prompt(LIMIT - 1)
        before = self.record()
        session = self.login(READ_ONLY_TOKEN)
        self.assertFalse(session.authenticated)
        self.assertTrue(session.closed)
        self.assertEqual(self.record(), before, "neither cleared nor counted")
        self.assertFalse(self.blocked())

    def test_a_superseded_token_login(self):
        self.promoted = adminchat._SUPERSEDED
        self.wrong_at_the_prompt(LIMIT - 1)
        before = self.record()
        session = self.login(FULL_TOKEN)
        self.assertFalse(session.authenticated)
        self.assertEqual(self.record(), before)


class UnlockFailuresOutliveTheSession(CountCase):

    def test_each_session_adds_to_the_address(self):
        for n in range(1, LIMIT):
            session = self.login(FULL_TOKEN)
            self.unlock(session, f"wrong {n}")
            self.assertEqual(session.unlock_failures, 1)
            self.assertEqual(self.record()[0], n)
            session.close(None)
        session = self.login(FULL_TOKEN)
        self.assertTrue(session.authenticated)
        self.unlock(session, "wrong again")
        self.assertTrue(self.blocked())
        self.assertFalse(self.login(FULL_TOKEN).authenticated)


class ReleasePasswordAttempt(CountCase):
    """release_password_attempt() on its own."""

    def reserve(self):
        counted, reservation = adminchat._reserve_attempt(ADDRESS)
        self.assertTrue(counted)
        self.assertIsNotNone(reservation)
        return reservation

    def test_no_record_before_none_after(self):
        reservation = self.reserve()
        adminchat.release_password_attempt(ADDRESS, reservation)
        self.assertIsNone(self.record())

    def test_a_block_the_attempt_set_is_lifted(self):
        adminchat._bad_ips[ADDRESS] = [LIMIT - 1, 0.0, time.time() - 60]
        kept = self.record()
        before = self.reserve()
        self.assertTrue(self.blocked(), "the attempt that reaches the limit sets the block")
        adminchat.release_password_attempt(ADDRESS, before)
        self.assertEqual(self.record(), kept)

    def test_another_attempt_counted_meanwhile_stays(self):
        adminchat._bad_ips[ADDRESS] = [1, 0.0, time.time() - 60]
        before = self.reserve()
        self.reserve()
        latest = self.record()[2]
        adminchat.release_password_attempt(ADDRESS, before)
        self.assertEqual(self.record()[0], 2)
        self.assertEqual(self.record()[2], latest, "the later attempt's time is kept")
        self.assertFalse(self.blocked())

    def test_cleared_meanwhile_stays_cleared(self):
        before = self.reserve()
        adminchat.clear_bad_ip(ADDRESS)
        adminchat.release_password_attempt(ADDRESS, before)
        self.assertIsNone(self.record())

    def test_a_record_made_again_meanwhile_is_left_alone(self):
        """Cleared, then counted again by a later attempt: the release takes
        nothing from the new record."""
        for earlier in (None, [1, 0.0, time.time() - 60]):
            with self.subTest(earlier=earlier):
                adminchat.reset_state_for_tests()
                if earlier:
                    adminchat._bad_ips[ADDRESS] = list(earlier)
                reservation = self.reserve()
                adminchat.clear_bad_ip(ADDRESS)
                self.reserve()
                again = self.record()
                self.assertEqual(again[0], 1)
                adminchat.release_password_attempt(ADDRESS, reservation)
                self.assertEqual(self.record(), again)

    def test_a_record_dropped_as_stale_and_made_again_is_left_alone(self):
        adminchat._bad_ips[ADDRESS] = [1, 0.0, time.time() - 60]
        reservation = self.reserve()
        adminchat._bad_ips[ADDRESS][2] = time.time() - adminchat.BAD_IP_BLOCK_SECONDS - 1
        self.reserve()                       # drops the stale record, counts on a new one
        again = self.record()
        self.assertEqual(again[0], 1)
        adminchat.release_password_attempt(ADDRESS, reservation)
        self.assertEqual(self.record(), again)

    def test_a_refused_attempt_has_nothing_to_release(self):
        adminchat._bad_ips[ADDRESS] = [LIMIT, time.time() + 60, time.time()]
        kept = self.record()
        counted, reservation = adminchat._reserve_attempt(ADDRESS)
        self.assertEqual((counted, reservation), (False, None))
        adminchat.release_password_attempt(ADDRESS, reservation)
        self.assertEqual(self.record(), kept, "a refused attempt takes nothing back")
        self.assertTrue(adminchat.reserve_password_attempt(ADDRESS) is False)

    def test_no_address(self):
        self.assertEqual(adminchat._reserve_attempt(""), (True, None))
        adminchat.release_password_attempt("", None)
        self.assertEqual(adminchat._bad_ips, {})


if __name__ == "__main__":
    import unittest
    unittest.main()
