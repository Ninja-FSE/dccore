"""A logout, or a password change, ends a dashboard session on the server.

The session used to be the cookie alone - {"authenticated": True}, signed
with a per-process key - and every route trusted any cookie with a valid
signature. A logout forgot the cookie in the browser that clicked; a password
change wrote a new hash and left the key alone. An operator who suspected a
copied cookie or a leaked password could do nothing short of restarting the
daemon: the copy kept full access, Settings and Console included.

Now each login gets an id the server must still hold (a logout drops it) and
a stamp of the password it was opened with (a password change makes every
older stamp wrong). The operator's own tab that changed the password stays
logged in.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_every_route_is_behind_the_login import PASSWORD, RouteCase  # noqa: E402


def session_cookie(response):
    """The session cookie's value from a response's Set-Cookie header - the
    same on every Flask version, unlike the test client's cookie API."""
    for header in response.headers.getlist("Set-Cookie"):
        name, _, rest = header.partition("=")
        if name.strip() == "session":
            return rest.split(";", 1)[0]
    raise AssertionError("the response set no session cookie")


class TheStamp(DCCoreTestCase):
    """_password_stamp() itself - no Flask."""

    def test_it_changes_with_the_password_hash(self):
        old = webserver._password_stamp(b"k" * 32, "pbkdf2$old")

        self.assertNotEqual(old, webserver._password_stamp(b"k" * 32, "pbkdf2$new"))
        self.assertEqual(old, webserver._password_stamp(b"k" * 32, "pbkdf2$old"))

    def test_it_changes_with_the_apps_secret(self):
        self.assertNotEqual(webserver._password_stamp(b"a" * 32, "pbkdf2$same"),
                            webserver._password_stamp(b"b" * 32, "pbkdf2$same"))

    def test_the_hash_itself_is_not_in_it(self):
        """The cookie is signed, not encrypted: the browser can read it."""
        stored = "pbkdf2_sha256$1000$salt$hash-material"

        self.assertNotIn("hash-material", webserver._password_stamp(b"k" * 32, stored))


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class ACopiedCookie(RouteCase):

    def setUp(self):
        super().setUp()
        self.settings_path = os.path.join(self.tree.root, "settings.conf")
        os.environ["DCCORE_SETTINGS_FILE"] = self.settings_path
        self.addCleanup(os.environ.pop, "DCCORE_SETTINGS_FILE", None)
        # A successful password change dispatches a real rehash otherwise.
        real_rehash = commands.handle_rehash_request
        commands.handle_rehash_request = lambda *a, **kw: None
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)

    def log_in_and_copy(self):
        resp = self.client.post("/login", data={"password": PASSWORD})
        self.assertEqual(resp.status_code, 302, "the fixture's own login failed")
        return session_cookie(resp)

    def with_cookie(self, cookie, path="/api/settings"):
        """A request from somewhere else that holds a copy of the cookie.
        use_cookies=False: a client with a cookie jar of its own replaces a
        Cookie header with the jar's (empty) contents."""
        return self.app.test_client(use_cookies=False).get(
            path, headers={"Cookie": "session=" + cookie})

    def test_the_copy_works_while_the_session_is_open(self):
        """Control: without this, every refusal below could be the copy
        never having worked."""
        cookie = self.log_in_and_copy()

        self.assertEqual(self.with_cookie(cookie).status_code, 200)

    def test_a_logout_ends_it_on_the_server(self):
        cookie = self.log_in_and_copy()

        self.assertEqual(self.client.post("/logout").status_code, 302)

        self.assertEqual(self.with_cookie(cookie).status_code, 401)
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_a_logout_ends_only_that_session(self):
        other = self.app.test_client()
        self.assertEqual(other.post("/login", data={"password": PASSWORD}).status_code, 302)
        self.log_in_and_copy()

        self.client.post("/logout")

        self.assertEqual(other.get("/api/settings").status_code, 200)

    def test_a_password_change_ends_every_other_session(self):
        cookie = self.log_in_and_copy()
        other = self.app.test_client()
        self.assertEqual(other.post("/login", data={"password": PASSWORD}).status_code, 302)

        resp = self.client.post("/api/settings/password", json={
            "new_password": "a-new-one-entirely", "confirm_password": "a-new-one-entirely"})
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(self.with_cookie(cookie).status_code, 401,
                         "a cookie from before the password change still works")
        self.assertEqual(other.get("/api/settings").status_code, 401)

    def test_the_tab_that_changed_the_password_stays_logged_in(self):
        self.log_in_and_copy()

        self.client.post("/api/settings/password", json={
            "new_password": "a-new-one-entirely", "confirm_password": "a-new-one-entirely"})

        self.assertEqual(self.client.get("/api/settings").status_code, 200)

    def test_the_new_password_logs_in_at_once(self):
        self.log_in_and_copy()
        self.client.post("/api/settings/password", json={
            "new_password": "a-new-one-entirely", "confirm_password": "a-new-one-entirely"})

        fresh = self.app.test_client()

        self.assertEqual(fresh.post("/login", data={"password": "a-new-one-entirely"}).status_code, 302)
        self.assertEqual(fresh.post("/login", data={"password": PASSWORD}).status_code, 401)

    def test_a_password_changed_in_the_file_ends_the_session_too(self):
        """settings.conf edited by hand and !rehash: the hash changes
        without the dashboard's route being involved at all."""
        cookie = self.log_in_and_copy()

        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash("from-the-file", iterations=1000)

        self.assertEqual(self.with_cookie(cookie).status_code, 401)

    def test_a_refused_cookie_is_dropped_from_the_browser(self):
        self.log_in_and_copy()
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash("from-the-file", iterations=1000)

        resp = self.client.get("/api/settings")

        self.assertEqual(resp.status_code, 401)
        self.assertTrue(any(h.startswith("session=;") or "Expires=Thu, 01 Jan 1970" in h
                            for h in resp.headers.getlist("Set-Cookie")),
                        "the stale cookie was not cleared")

    def test_the_oldest_session_goes_past_the_limit(self):
        real = webserver.WEB_MAX_LIVE_SESSIONS
        webserver.WEB_MAX_LIVE_SESSIONS = 2
        self.addCleanup(setattr, webserver, "WEB_MAX_LIVE_SESSIONS", real)
        first = self.log_in_and_copy()
        second = self.log_in_and_copy()
        third = self.log_in_and_copy()

        self.assertEqual(self.with_cookie(first).status_code, 401)
        self.assertEqual(self.with_cookie(second).status_code, 200)
        self.assertEqual(self.with_cookie(third).status_code, 200)

    def test_the_settings_file_got_the_new_hash(self):
        """The change still reaches settings.conf, where the rehash and the
        next start read it."""
        self.log_in_and_copy()
        self.client.post("/api/settings/password", json={
            "new_password": "a-new-one-entirely", "confirm_password": "a-new-one-entirely"})

        with io.open(self.settings_path, encoding="utf-8") as handle:
            self.assertIn(config.ADMIN_PASSWORD_HASH, handle.read())


if __name__ == "__main__":
    unittest.main()
