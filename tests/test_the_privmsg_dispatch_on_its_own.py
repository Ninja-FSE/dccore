"""#1334: irc.answer_privmsg(), the PRIVMSG dispatch, called directly.

It was the body of irc_loop()'s `if privmsg_parsed:`, the loop's last and
largest branch; step 6 of splitting the loop made it a function, its
`continue`s `return`s. Most of these call it with one parsed line and read
back what it did - and that the `return`s end the line's handling where the
`continue`s did; the last drives the real loop to check the wiring.

Every nick, channel and host here is invented.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase, install_fake_oserve, silence_debug  # noqa: E402
from tests.test_the_bots_own_nick_follows_the_server import (  # noqa: E402
    NOTICE_AUTH, DrivesPastRegistration, welcome)


class Dispatch(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="alfa", ORIGINAL_NICK="alfa", CHANNEL="#somechannel", DEBUG_CHANNEL="")
        silence_debug(announce)
        self.oserve = install_fake_oserve()
        self.sock = object()

    def send(self, line):
        parsed = irc.parse_privmsg(line)
        self.assertIsNotNone(parsed, line)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            irc.answer_privmsg(line, parsed, self.sock, self.oserve)
        return said.getvalue()

    def queued_to(self, nick):
        return [msg for user, msg, *_ in self.oserve.queued if user == nick]


class CTCP(Dispatch):

    def test_a_version_request_is_answered_through_the_passed_oserve(self):
        self.send(":bravo!b@host.example PRIVMSG alfa :\x01VERSION\x01")
        replies = self.queued_to("bravo")
        self.assertEqual(len(replies), 1, self.oserve.queued)
        self.assertIn("VERSION", replies[0])

    def test_without_oserve_nothing_is_sent_and_nothing_raises(self):
        parsed = irc.parse_privmsg(":bravo!b@host.example PRIVMSG alfa :\x01VERSION\x01")
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_privmsg(":bravo!b@host.example PRIVMSG alfa :\x01VERSION\x01", parsed, self.sock, None)
        self.assertEqual(self.oserve.queued, [])


class TheDiagnosticsGate(Dispatch):
    """The gate's `return` - a `continue` until #1334 - must still end the
    line: a stranger's !ping is not answered."""

    def started(self, gate_open):
        """What the dispatch starts for a !ping, recorded rather than run:
        the answer goes out on a thread, so a missing gate would otherwise
        show only after the assertion had passed."""
        import threading
        import types
        started = []

        class Recorded:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                self.target = target

            def start(self):
                started.append(self.target)

        stub = types.ModuleType("threading")
        stub.__dict__.update(threading.__dict__)
        stub.Thread = Recorded
        real_threading = irc.threading
        irc.threading = stub
        self.addCleanup(setattr, irc, "threading", real_threading)
        real_gate = commands.diagnostics_are_for_the_admin
        commands.diagnostics_are_for_the_admin = lambda user, host: gate_open
        self.addCleanup(setattr, commands, "diagnostics_are_for_the_admin", real_gate)

        self.send(":bravo!b@host.example PRIVMSG #somechannel :!ping")
        return started

    def test_a_strangers_ping_goes_no_further(self):
        self.assertNotIn(commands.handle_ping_request, self.started(gate_open=False))

    def test_the_admins_ping_is_answered(self):
        """Control: without it, a dispatch that never answered !ping at all
        would pass the test above."""
        self.assertIn(commands.handle_ping_request, self.started(gate_open=True))


class TheLoopHandsEveryPrivmsgOver(DrivesPastRegistration):
    """The real read loop: a PRIVMSG after registration reaches
    answer_privmsg() with the line, this connection's socket and the oserve
    the loop looked up - not None, which would silently answer nothing."""

    def test_the_line_and_the_loops_oserve_are_passed(self):
        stub = install_fake_oserve()
        seen = []
        real = irc.answer_privmsg

        def spy(line, privmsg_parsed, sock, oserve):
            seen.append((line, oserve))
        irc.answer_privmsg = spy
        self.addCleanup(setattr, irc, "answer_privmsg", real)

        line = ":bravo!b@host.example PRIVMSG SomeBot :\x01VERSION\x01"
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_registration(NOTICE_AUTH, welcome("SomeBot"), line)

        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(seen[0][0], line)
        self.assertIs(seen[0][1], stub)


if __name__ == "__main__":
    unittest.main()
