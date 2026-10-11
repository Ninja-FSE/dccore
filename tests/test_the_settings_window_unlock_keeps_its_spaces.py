"""#1281: the settings window's Unlock lost the spaces in a password.

#1273 made a password typed at the console login keep a run of spaces and a
space at either end. The settings window's unlock prompt (#1264) still put
the answer in a /var and sent `unlock %pw`, and mIRC closes those spaces up in
a /var and in a command's parameters alike: a password that has them could log
in but never unlock.

The window now hands $input's answer straight to $dccore.sw.unlockarg, in one
nested expression, and sends such a password encoded the way it encodes a
value; `unlock` tries the argument as it is, then decoded - one attempt, not
two. Any other password goes exactly as typed, which a bot from before this
checks as it is.

The script side is read from dccore.mrc with its comment lines left out, as
tests/test_a_typed_password_keeps_its_spaces.py reads the login's.
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

SPACED = [" lead", "trail ", "two  inner", "  both   ends  ", "a %20 b  ", " %admin% ", "   "]
PLAIN = ["plain", "two words", "%20 stays", "a%20b", "50%off", "unlock me"]


def mirc_unlockarg(typed):
    """What $dccore.sw.unlockarg returns for a password typed as `typed`, read
    off the alias statement by statement. $1 is the identifier's argument:
    the text as typed."""
    body = settings_window.statements(settings_window.alias("dccore.sw.unlockarg"))
    test = re.fullmatch(r"if \(\$regex\(\$1,/(.+)/\)\) \{ return \$dccore\.sw\.enc\(\$1\) \}", body[0])
    assert test is not None and body[1:] == ["return $1"], body
    if re.search(test.group(1), typed):
        return settings_window.mirc_enc(typed)
    return typed


def mirc_unlock_line(typed):
    """The console line the window sends for `typed`: unlockarg's answer
    becomes unlocksend's parameters ($1-, closed up), sent after "unlock"."""
    ask = settings_window.statements(settings_window.alias("dccore.sw.unlockask"))[-1]
    assert ask.startswith("dccore.sw.unlocksend $dccore.sw.unlockarg($input("), ask
    assert settings_window.statements(settings_window.alias("dccore.sw.unlocksend"))[-1] == \
        "dccore.send unlock $1-"
    return "unlock " + settings_window.collapse(mirc_unlockarg(typed))


class TheScript(unittest.TestCase):

    def test_the_answer_is_never_put_in_a_variable(self):
        """The one construct that cannot close the spaces up: $input's result
        is an identifier's argument from the moment it exists."""
        (guard, ask) = settings_window.statements(settings_window.alias("dccore.sw.unlockask"))
        self.assertEqual(guard, "if (!$dialog(dccore.set)) { return }")
        self.assertRegex(ask, r"^dccore\.sw\.unlocksend \$dccore\.sw\.unlockarg\(\$input\([^,]+,po,DCCore - Unlock\)\)$")
        for name in ("dccore.sw.unlockask", "dccore.sw.unlockarg", "dccore.sw.unlocksend"):
            self.assertNotRegex(settings_window.alias(name), r"\bvar\b|%\w", name)

    def test_a_spaced_password_survives_the_trip_to_the_bot(self):
        for typed in SPACED:
            with self.subTest(typed=typed):
                line = mirc_unlock_line(typed)
                argument = line[len("unlock "):]
                self.assertNotEqual(argument, typed, "sent as typed, it would be closed up")
                self.assertEqual(argument, mirc_unlockarg(typed), "mIRC would change it on the way")
                self.assertEqual(console_settings.decode_value(argument), typed)

    def test_any_other_password_goes_as_typed(self):
        """What a bot from before #1281 checks, as it is."""
        for typed in PLAIN:
            with self.subTest(typed=typed):
                self.assertEqual(mirc_unlock_line(typed), "unlock " + typed)

    def test_a_cancelled_box_still_drops_the_save(self):
        self.assertEqual(mirc_unlockarg(""), "")
        first = settings_window.statements(settings_window.alias("dccore.sw.unlocksend"))[0]
        self.assertEqual(first, "if ($1- == $null) { dccore.sw.unlockdrop Not saved: no password was given. | return }")


class TheBot(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(adminchat.reset_state_for_tests)
        adminchat.WRONG_PASSWORD_DELAY, real = 0.0, adminchat.WRONG_PASSWORD_DELAY
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", real)
        # Every count against the address, however it is made: a failure
        # noted after the verify, or an attempt reserved before it.
        self.counted = []
        real_note = adminchat.note_bad_ip
        self.addCleanup(setattr, adminchat, "note_bad_ip", real_note)
        real_reserve = adminchat.reserve_password_attempt
        self.addCleanup(setattr, adminchat, "reserve_password_attempt", real_reserve)

        def note_bad_ip(ip):
            self.counted.append(ip)
            return real_note(ip)

        def reserve_password_attempt(ip):
            self.counted.append(ip)
            return real_reserve(ip)
        adminchat.note_bad_ip = note_bad_ip
        adminchat.reserve_password_attempt = reserve_password_attempt

    def use(self, password):
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(password, iterations=1000))

    def token_session(self):
        """A console that logged in with a paired token: locked."""
        session = adminchat.Session(socket.socket(), "192.0.2.41", "alfa", "alfa.users.example")
        self.addCleanup(session.close, None)
        session.authenticated = True
        session.unlocked = False
        return session

    def send(self, session, line):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            adminchat.handle_command(session, line)
        return out.getvalue()

    def test_what_the_window_sends_unlocks(self):
        for password in SPACED + PLAIN:
            with self.subTest(password=password):
                self.use(password)
                session = self.token_session()
                self.send(session, mirc_unlock_line(password))
                self.assertTrue(session.unlocked)
                self.assertEqual(session.unlock_failures, 0)

    def test_what_the_window_used_to_send_is_still_refused(self):
        self.use(" two  inner ")
        session = self.token_session()
        self.send(session, "unlock two inner")
        self.assertFalse(session.unlocked)
        self.assertEqual(session.unlock_failures, 1)

    def test_a_wrong_encoded_password_is_one_attempt_not_two(self):
        self.use(" two  inner ")
        session = self.token_session()
        self.send(session, "unlock %20two inner%20")
        self.assertFalse(session.unlocked)
        self.assertEqual(session.unlock_failures, 1)
        self.assertEqual(self.counted, ["192.0.2.41"], "one wrong unlock counted against the address once")

    def test_a_password_that_looks_encoded_unlocks_as_typed(self):
        """Tried as it is first: an ordinary password that happens to hold an
        escape is never mistaken for its decoding."""
        for password in ("a%20b", "%2520", "x%41y"):
            with self.subTest(password=password):
                self.use(password)
                session = self.token_session()
                self.send(session, "unlock " + password)
                self.assertTrue(session.unlocked)

    def test_the_password_is_not_logged(self):
        self.use("  both   ends  ")
        session = self.token_session()
        log = self.send(session, mirc_unlock_line("  both   ends  "))
        self.assertTrue(session.unlocked)
        self.assertNotIn("both", log)
        log = self.send(self.token_session(), "unlock %20wrong%20%20one")
        self.assertIn("wrong unlock password", log)
        self.assertNotIn("%20wrong", log)
        self.assertNotIn(" wrong  one", log)


if __name__ == "__main__":
    unittest.main()
