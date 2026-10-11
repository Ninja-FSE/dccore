"""Parallel logins from one address get no more guesses than the limit.

The dashboard login checked whether the address was blocked, spent a tenth
of a second hashing the password, and only then counted a failure. Nothing
was held in between, and the server gives every connection its own thread,
so a burst of POST /login from one address all passed the check before any
of them was counted: MAX_PASSWORD_ATTEMPTS guesses per block window became
as many guesses as could be sent at once.

The login now claims its attempt BEFORE the password is hashed
(_reserve_web_login) and settles it after (_settle_web_login), both under
runtime.web_login_lock. And no more than runtime.WEB_PASSWORD_CHECKS_AT_ONCE
passwords are hashed at a time across the dashboard, so a flood of logins
cannot take the CPU from the transfers either.

The pure half runs everywhere; the HTTP half needs Flask.
"""

import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_every_route_is_behind_the_login import PASSWORD, RouteCase  # noqa: E402

ADDRESS = "203.0.113.7"
BURST = 8


class TheClaimIsOneStep(DCCoreTestCase):
    """_reserve_web_login() itself, with plain threads - no Flask."""

    def setUp(self):
        super().setUp()
        webserver._web_bad_ips.clear()
        webserver._web_logins_in_flight.clear()
        self.addCleanup(webserver._web_bad_ips.clear)
        self.addCleanup(webserver._web_logins_in_flight.clear)

    def test_a_burst_from_one_address_claims_only_the_allowance(self):
        barrier = threading.Barrier(BURST)
        answers = []
        answers_lock = threading.Lock()

        def claim():
            barrier.wait(timeout=10)
            answer = webserver._reserve_web_login(ADDRESS)
            with answers_lock:
                answers.append(answer)

        threads = [threading.Thread(target=claim, daemon=True) for _ in range(BURST)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(answers), BURST, "a claiming thread never finished")
        self.assertEqual(answers.count("ok"), adminchat.MAX_PASSWORD_ATTEMPTS)
        self.assertEqual(answers.count("busy"), BURST - adminchat.MAX_PASSWORD_ATTEMPTS)

    def test_earlier_failures_shrink_the_allowance(self):
        webserver._note_bad_web_login(ADDRESS)

        answers = [webserver._reserve_web_login(ADDRESS) for _ in range(BURST)]

        self.assertEqual(answers.count("ok"), adminchat.MAX_PASSWORD_ATTEMPTS - 1)

    def test_settled_failures_block_the_address(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.assertEqual(webserver._reserve_web_login(ADDRESS), "ok")
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            webserver._settle_web_login(ADDRESS, False)

        self.assertEqual(webserver._reserve_web_login(ADDRESS), "blocked")
        self.assertTrue(webserver._is_bad_web_ip(ADDRESS))
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_an_attempt_never_checked_is_released_uncounted(self):
        self.assertEqual(webserver._reserve_web_login(ADDRESS), "ok")
        webserver._settle_web_login(ADDRESS, None)

        self.assertEqual(webserver._web_logins_in_flight, {})
        self.assertNotIn(ADDRESS, webserver._web_bad_ips)

    def test_a_right_password_forgets_the_failures(self):
        webserver._note_bad_web_login(ADDRESS)
        self.assertEqual(webserver._reserve_web_login(ADDRESS), "ok")
        webserver._settle_web_login(ADDRESS, True)

        self.assertNotIn(ADDRESS, webserver._web_bad_ips)

    def test_the_lock_is_runtimes(self):
        """A lock built in webserver.py would be the one rule every other
        lock in the daemon follows, broken."""
        self.assertIs(webserver._web_bad_ips_lock, runtime.web_login_lock)


class OnlyAFewPasswordsAreHashedAtOnce(DCCoreTestCase):

    def test_with_every_slot_taken_the_password_is_not_checked(self):
        checked = []
        real_verify = adminchat.verify_password
        adminchat.verify_password = lambda stored, supplied: checked.append(supplied) or True
        self.addCleanup(setattr, adminchat, "verify_password", real_verify)

        taken = 0
        while runtime.web_password_checks.acquire(blocking=False):
            taken += 1
        try:
            self.assertEqual(taken, runtime.WEB_PASSWORD_CHECKS_AT_ONCE)
            self.assertIsNone(webserver._check_web_password("hash", "guess", wait=0.01))
            self.assertEqual(checked, [])
        finally:
            for _ in range(taken):
                runtime.web_password_checks.release()

        self.assertTrue(webserver._check_web_password("hash", "guess", wait=0.01))
        self.assertEqual(checked, ["guess"])


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class ABurstOfLoginPosts(RouteCase):
    """The audit's reproduction through the real login route: parallel
    POST /login from one address, each with a wrong password."""

    def setUp(self):
        super().setUp()
        webserver._web_logins_in_flight.clear()
        self.addCleanup(webserver._web_bad_ips.clear)
        self.addCleanup(webserver._web_logins_in_flight.clear)
        # Enough hashing slots for the whole burst: what is under test here
        # is the per-address allowance, not the global cap (tested above).
        real_slots = runtime.web_password_checks
        runtime.web_password_checks = threading.BoundedSemaphore(BURST)
        self.addCleanup(setattr, runtime, "web_password_checks", real_slots)

    def test_a_burst_from_one_address_checks_no_more_passwords_than_the_limit(self):
        # Every password check is held until each request of the burst has
        # either reached the check or been answered without it - so on the
        # old code all of them pile up inside the check, as they did with
        # real PBKDF2, and on the new one the extra ones are turned away.
        state = threading.Condition()
        checked, answered = [], []

        def held_verify(stored, supplied):
            with state:
                checked.append(supplied)
                state.notify_all()
                state.wait_for(lambda: len(checked) + len(answered) >= BURST, timeout=10)
            return False

        real_verify = adminchat.verify_password
        adminchat.verify_password = held_verify
        self.addCleanup(setattr, adminchat, "verify_password", real_verify)

        statuses = []
        barrier = threading.Barrier(BURST)

        def guess(number):
            client = self.app.test_client()
            barrier.wait(timeout=10)
            resp = client.post("/login", data={"password": "guess-%d" % number},
                               environ_base={"REMOTE_ADDR": ADDRESS})
            with state:
                statuses.append(resp.status_code)
                answered.append(number)
                state.notify_all()

        threads = [threading.Thread(target=guess, args=(n,), daemon=True) for n in range(BURST)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(statuses), BURST, "a login request never finished")
        self.assertEqual(len(checked), adminchat.MAX_PASSWORD_ATTEMPTS,
                         "a burst from one address had more passwords checked "
                         "than the failed-login limit allows")
        self.assertEqual(statuses.count(401), adminchat.MAX_PASSWORD_ATTEMPTS)
        self.assertEqual(statuses.count(429), BURST - adminchat.MAX_PASSWORD_ATTEMPTS)
        self.assertTrue(webserver._is_bad_web_ip(ADDRESS))
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_when_the_dashboard_is_busy_the_login_is_not_counted(self):
        real_slots = runtime.web_password_checks
        runtime.web_password_checks = threading.BoundedSemaphore(1)
        self.addCleanup(setattr, runtime, "web_password_checks", real_slots)
        real_wait = webserver.WEB_PASSWORD_CHECK_WAIT_SECONDS
        webserver.WEB_PASSWORD_CHECK_WAIT_SECONDS = 0.01
        self.addCleanup(setattr, webserver, "WEB_PASSWORD_CHECK_WAIT_SECONDS", real_wait)
        self.assertTrue(runtime.web_password_checks.acquire(blocking=False))
        try:
            resp = self.client.post("/login", data={"password": PASSWORD})
        finally:
            runtime.web_password_checks.release()

        self.assertEqual(resp.status_code, 503)
        self.assertIn(b"busy", resp.data)
        self.assertEqual(webserver._web_bad_ips, {})
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_the_operator_still_logs_in(self):
        """Control: one login at a time is untouched."""
        self.log_in()
        self.assertEqual(self.client.get("/api/queue").status_code, 200)


if __name__ == "__main__":
    unittest.main()
