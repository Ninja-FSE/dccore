"""#581: `pair` in the dashboard Console minted a token nobody could read.

`pair` and `hello` are listed by the Console's own `help`, and both dispatch to
handlers written for a DCC CHAT Session. The web shim had only .send/.nick/
.last_activity: `pair` saved the new hash to disk and THEN read session.structured
(AttributeError), so the Console said "Command failed" and the token was never
shown - while any script already paired under that name was silently locked out.
`hello` printed its DCCORE HELLO line and failed one line later.

`pair` now works on the shim (always prose), `hello` is declined up front like
`quit`, and a structural test fails if a handler ever reads a session attribute
the shim lacks.
"""

import inspect
import os
import re
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import db  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class WithATokenStore(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-pair-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        real = db.ADMIN_TOKENS_FILE
        db.ADMIN_TOKENS_FILE = os.path.join(self.tmp, "tokens.json")
        self.addCleanup(setattr, db, "ADMIN_TOKENS_FILE", real)

    def run_command(self, text):
        status, result = webserver.build_console_command_result(text, "127.0.0.1")
        self.assertEqual(status, 200)
        return result["lines"]


class PairInTheConsole(WithATokenStore):

    def test_the_token_is_shown(self):
        lines = self.run_command("pair dccore.mrc 1.0")

        self.assertFalse(any("Command failed" in line for line in lines), lines)
        shown = [line.strip() for line in lines if line.startswith("  ") and line.strip()]
        self.assertEqual(len(shown), 1, lines)
        self.assertGreaterEqual(len(shown[0]), 32)

    def test_the_token_shown_is_the_one_that_was_stored(self):
        lines = self.run_command("pair dccore.mrc")
        token = [line.strip() for line in lines if line.startswith("  ")][0]

        stored = db.load_admin_tokens()["dccore.mrc"]
        self.assertTrue(adminchat.verify_password(stored["hash"], token))
        self.assertEqual(stored["by"], "web:127.0.0.1")

    def test_it_says_how_to_revoke_it(self):
        self.assertIn("unpair dccore.mrc", " ".join(self.run_command("pair dccore.mrc")))

    def test_pairing_a_name_again_says_the_old_token_is_gone(self):
        first = self.run_command("pair dccore.mrc")
        self.assertNotIn("replaced", " ".join(first))

        second = self.run_command("pair dccore.mrc")

        self.assertIn("replaced the token dccore.mrc had before", " ".join(second))

    def test_a_new_name_leaves_the_others_alone(self):
        self.run_command("pair one")
        before = db.load_admin_tokens()["one"]["hash"]
        self.run_command("pair two")
        self.assertEqual(db.load_admin_tokens()["one"]["hash"], before)


class HelloInTheConsole(WithATokenStore):

    def test_it_is_declined_with_a_reason_not_half_run(self):
        lines = self.run_command("hello dccore.mrc 1.0")

        self.assertEqual(len(lines), 1)
        self.assertNotIn("DCCORE HELLO", lines[0])
        self.assertNotIn("Command failed", lines[0])
        self.assertIn("structured feed", lines[0])

    def test_it_never_reaches_the_handler(self):
        called = []
        real = adminchat._cmd_hello
        adminchat.COMMANDS["hello"] = (lambda *a: called.append(a),) + adminchat.COMMANDS["hello"][1:]
        self.addCleanup(adminchat.COMMANDS.__setitem__, "hello", (real,) + adminchat.COMMANDS["hello"][1:])
        self.run_command("hello dccore.mrc 1.0")
        self.assertEqual(called, [])

    def test_quit_keeps_its_own_message(self):
        self.assertIn("close this tab", self.run_command("quit")[0])


class TheShimHasWhatTheHandlersRead(unittest.TestCase):
    """A handler that reads a Session attribute the web shim lacks is the
    #581 bug again, and it shows up as "Command failed" only when somebody
    types the command. Found here instead."""

    def test_every_supported_command_reads_only_what_the_shim_has(self):
        shim = webserver._WebConsoleSession("web:test")
        have = {name for name in dir(shim) if not name.startswith("__")}
        for name, (handler, _summary, _usage) in adminchat.COMMANDS.items():
            if name in webserver._CONSOLE_UNSUPPORTED_COMMANDS:
                continue
            used = set(re.findall(r"\bsession\.([A-Za-z_]+)", inspect.getsource(handler)))
            self.assertLessEqual(used, have, f"{name!r} reads {sorted(used - have)} from the session")

    def test_every_unsupported_command_has_a_message(self):
        for name in webserver._CONSOLE_UNSUPPORTED_COMMANDS:
            self.assertIn(name, webserver._CONSOLE_UNSUPPORTED_MESSAGES)

    def test_the_shim_is_always_prose(self):
        self.assertFalse(webserver._WebConsoleSession("x").structured)


if __name__ == "__main__":
    unittest.main()
