"""#622: the setup page, POST /api/settings/password and the dashboard's login
all hash and verify the password exactly as typed. The DCC CHAT console used
to strip the line before verifying it, so a password with a leading or
trailing space opened the dashboard and never the console - three refusals,
a blocked address, and nothing saying why.

The console now checks the line verbatim, so the two doors agree on what the
password is.
"""

import contextlib
import io
import os
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

PASSWORD = " swordfish "


class APasswordWithSurroundingSpaces(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.stored = adminchat.make_password_hash(PASSWORD, iterations=1000)
        self.set_config(ADMIN_PASSWORD_HASH=self.stored)
        adminchat.WRONG_PASSWORD_DELAY, real = 0.0, adminchat.WRONG_PASSWORD_DELAY
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", real)

    def login(self, line):
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(s, line)
        return s

    def test_opens_the_dashboard(self):
        """What webserver's login does with the form value: verify it as is."""
        self.assertTrue(adminchat.verify_password(self.stored, PASSWORD))

    def test_opens_the_console_too(self):
        s = self.login(PASSWORD)
        self.assertTrue(s.authenticated)
        self.assertEqual(s.attempts, 0)

    def test_the_console_refuses_what_the_dashboard_refuses(self):
        """The stripped form is a different password on the dashboard, so it
        is a different password here - one door, one meaning."""
        self.assertFalse(adminchat.verify_password(self.stored, PASSWORD.strip()))
        s = self.login(PASSWORD.strip())
        self.assertFalse(s.authenticated)
        self.assertEqual(s.attempts, 1)

    def test_a_stray_enter_is_not_an_attempt(self):
        s = self.login("")
        self.assertFalse(s.authenticated)
        self.assertEqual(s.attempts, 0)


if __name__ == "__main__":
    unittest.main()
