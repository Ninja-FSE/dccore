"""Neither side checked the other's version, so a stale script copy
misparsed silently (audit L45, #709).

The channel field went into every event line without a number moving.
#795 gave HELLO a minor and taught the script to check it. This is the
other direction: `hello <client> <version>` carries the script's own
version, and the bot logged it and nothing more - an operator who pulled
the bot but not the script (a file copied into mIRC's folder by hand) got
REQUEST lines reading "bob asked for file Artist/Album" and SENDING slot
numbers showing the channel, with nothing saying why. MIN_SCRIPT_VERSION
names the oldest script that reads this bot's lines right, and a hello
from an older one - or one with no version the bot can read - is answered
with a line in the window saying to update the script. Structured mode is
still switched on: the script keeps parsing, and the major is the same.
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

from tests.support import DCCoreTestCase  # noqa: E402


class TheComparison(unittest.TestCase):

    def test_the_minimum_is_the_script_that_knows_the_channel_field(self):
        self.assertEqual(adminchat.MIN_SCRIPT_VERSION, "1.1")

    def test_older_is_too_old_and_newer_is_not(self):
        self.assertTrue(adminchat.script_is_too_old("1.0"))
        self.assertFalse(adminchat.script_is_too_old("1.1"))
        self.assertFalse(adminchat.script_is_too_old("1.2"))
        self.assertFalse(adminchat.script_is_too_old("2.0"))
        self.assertFalse(adminchat.script_is_too_old("1.10"), '"1.10" is ten, not one')

    def test_no_version_or_junk_is_too_old(self):
        for value in ("", None, "one", "1.x", "1..1"):
            self.assertTrue(adminchat.script_is_too_old(value), repr(value))


class AHelloFromAnOldScript(DCCoreTestCase):

    def session(self):
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = True
        return s

    def hello(self, args):
        s = self.session()
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_command(s, "hello " + args)
        return s, [l for l in s._outbox if not l.startswith(("DCCORE STATUS ", "DCCORE SLOT ", "DCCORE QUEUE "))]

    def test_the_pre_field_script_is_told_to_update_right_after_hello(self):
        s, lines = self.hello("dccore.mrc 1.0")

        self.assertTrue(s.structured, "structured mode is still switched on")
        self.assertTrue(lines[0].startswith("DCCORE HELLO 1.1 "), lines)
        self.assertTrue(lines[1].startswith("DCCORE OUT This dccore.mrc is version 1.0; this bot's lines are for 1.1 or later."), lines)
        self.assertIn("Update the script", lines[1])

    def test_the_current_script_is_not_nagged(self):
        _s, lines = self.hello("dccore.mrc 1.1")

        self.assertFalse(any("Update the script" in l for l in lines), lines)

    def test_a_hello_with_no_version_is_told_too(self):
        _s, lines = self.hello("someclient")

        self.assertTrue(any("is version unknown; this bot's lines are for 1.1 or later" in l for l in lines), lines)

    def test_the_console_log_says_so(self):
        s = self.session()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            adminchat.handle_command(s, "hello dccore.mrc 1.0")

        self.assertIn("dccore.mrc is 1.0; 1.1 or later reads this bot's lines", out.getvalue())


class TheScriptShipsAtTheMinimum(unittest.TestCase):

    def test_the_shipped_script_is_not_too_old_for_its_own_bot(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="utf-8") as handle:
            script = handle.read()
        import re
        version = re.search(r"alias dccore\.ver \{ return ([\d.]+) \}", script).group(1)

        self.assertFalse(adminchat.script_is_too_old(version), version)


if __name__ == "__main__":
    unittest.main()
