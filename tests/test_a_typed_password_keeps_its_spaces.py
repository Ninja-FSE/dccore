"""#1273: dccore.mrc could not send a password with runs of spaces in it.

#622 made the console verify the password exactly as typed, since the setup
page and the dashboard accept one with leading, trailing or doubled spaces. But
the script sent what was typed at its prompt as $1-, which closes runs of spaces
up and drops the ones at either end: the bot got "a b" for " a  b ", answered
"Incorrect Password." three times and blocked the address, while the same
password opened the dashboard. Pairing needs that typed password once, so such
an operator could not pair the script at all.

The script now reads such a password from the editbox as typed and sends it as
`DCCORE PASSWORD <value>`, its spaces escaped the way the settings window
escapes a value, and the bot decodes it. Any other password still goes as it
is typed, which a bot from before this accepts.
"""

import contextlib
import io
import re
import socket
import unittest

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase  # noqa: E402

import adminchat  # noqa: E402
import console_settings  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_mirc_settings_window as settings_window  # noqa: E402

SPACED = [" lead", "trail ", "two  inner", "  both   ends  ", "a %20 b  ", " %admin% "]
PLAIN = ["plain", "two words", "%20 stays", "DCCORE PASSWORD x"]


def password_branch():
    """The statements on INPUT:@DCCore runs for a password, comments left out."""
    source = settings_window.code()
    start = source.index("on *:INPUT:@DCCore: {")
    branch = source.index("if ($dccore.st(state) == password) {", start)
    return settings_window.statements(settings_window.block(
        "if ($dccore.st(state) == password) {", source[branch:]))


def mirc_sendpass(typed):
    """What `dccore.sendpass $1-` sends for a line typed as `typed`, read
    off the alias statement by statement: $1- is what a command's parameters
    make of it, $editbox(@DCCore) the line as typed."""
    body = settings_window.statements(settings_window.alias("dccore.sendpass"))
    assert body == [
        "if ($gettok($editbox(@DCCore),1-,32) === $1-) && ($len($editbox(@DCCore)) > $len($1-)) {",
        "dccore.send DCCORE PASSWORD $dccore.sw.enc($editbox(@DCCore))",
        "return",
        "}",
        "dccore.send $1-",
    ], body
    params = settings_window.collapse(typed)
    gettok = " ".join(token for token in typed.split(" ") if token)
    if gettok == params and len(typed) > len(params):
        return "DCCORE PASSWORD " + settings_window.mirc_enc(typed)
    return params


class TheScript(unittest.TestCase):

    def test_the_password_prompt_sends_through_sendpass(self):
        branch = password_branch()
        self.assertIn("dccore.sendpass $1-", branch)
        self.assertNotIn("dccore.send $1-", branch)
        self.assertIn("dccore.echo $dccore.prompt ********", branch)

    def test_a_spaced_password_goes_encoded_and_comes_back_as_typed(self):
        for typed in SPACED:
            with self.subTest(typed=typed):
                line = mirc_sendpass(typed)
                self.assertTrue(line.startswith("DCCORE PASSWORD "), line)
                self.assertNotRegex(line, "  |^ | $", "the line itself must not need its spaces kept")
                self.assertEqual(adminchat._spaced_password(line), typed)

    def test_any_other_password_goes_as_it_always_did(self):
        for typed in PLAIN:
            with self.subTest(typed=typed):
                self.assertEqual(mirc_sendpass(typed), typed)

    def test_the_script_says_its_new_version(self):
        version = re.search(r"\nalias dccore\.ver \{ return (\d+)\.(\d+)\.(\d+) \}\n", settings_window.code())
        self.assertGreaterEqual(tuple(int(part) for part in version.groups()), (1, 19, 1))


class TheBot(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(adminchat.reset_state_for_tests)
        adminchat.WRONG_PASSWORD_DELAY, real = 0.0, adminchat.WRONG_PASSWORD_DELAY
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", real)

    def use(self, password):
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(password, iterations=1000))

    def login(self, line):
        session = adminchat.Session(socket.socket(), "192.0.2.40", "alfa", "alfa.users.example")
        self.addCleanup(session.close, None)
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(session, line)
        return session

    def test_an_encoded_password_opens_the_console(self):
        for password in SPACED:
            with self.subTest(password=password):
                self.use(password)
                session = self.login("DCCORE PASSWORD " + console_settings.encode_value(password))
                self.assertTrue(session.authenticated)
                self.assertTrue(session.unlocked, "it is the password, not a token")
                self.assertEqual(session.attempts, 0)

    def test_what_mirc_used_to_send_is_still_refused(self):
        self.use(" two  inner ")
        session = self.login("two inner")
        self.assertFalse(session.authenticated)
        self.assertEqual(session.attempts, 1)

    def test_a_wrong_encoded_password_is_an_attempt(self):
        self.use(" two  inner ")
        session = self.login("DCCORE PASSWORD %20two inner%20")
        self.assertFalse(session.authenticated)
        self.assertEqual(session.attempts, 1)

    def test_a_password_that_looks_like_the_form_still_works_as_typed(self):
        self.use("DCCORE PASSWORD %20x")
        session = self.login("DCCORE PASSWORD %20x")
        self.assertTrue(session.authenticated)


if __name__ == "__main__":
    unittest.main()
