"""A dropped link reports what the server last said.

From a beta, after several channels were added:

    [DISCONNECT FIX] TCP keepalive detected a dead network ([WinError 10054]
    ...). Dropping the link to reconnect.

Two things were wrong with that line, and the second is why it could not be
chased. By the time it was asked about, the surrounding lines were gone and it
would not reproduce - so there was nothing to diagnose from and no way to ask
for more.

WHAT THE SERVER SAYS BEFORE IT HANGS UP. An ircd states its reason first:

    ERROR :Closing Link: SomeBot[host] (Max SendQ exceeded)

That line was being read, matched by nothing, and dropped. The only reference
to "ERROR" in the read loop was a condition inside a DEBUG_MODE filter - a
setting nobody has switched on when the thing they need it for happens.

WINERROR 10054 IS NOT A DEAD NETWORK. It is ECONNRESET: the server hanging up
on US. Reporting every socket error as "TCP keepalive detected a dead network"
sent an operator looking at their connection when the answer had been on the
wire a moment earlier.

The ring is small on purpose. The point is the handful of lines around a drop,
not a transcript, and it is held for the life of a connection on a bot that
may run for months without one.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402


class Captured:
    """Collects what _report_recent_lines() prints."""

    def __init__(self, test):
        self.lines = []
        self.test = test

    def __enter__(self):
        import builtins
        self._real = builtins.print
        builtins.print = lambda *a, **k: self.lines.append(
            " ".join(str(x) for x in a))
        return self

    def __exit__(self, *exc):
        import builtins
        builtins.print = self._real
        return False

    def text(self):
        return "\n".join(self.lines)


class TheTailIsReported(unittest.TestCase):

    def report(self, lines):
        with Captured(self) as out:
            irc._report_recent_lines(lines)
        return out.text()

    def test_the_servers_own_reason_survives_to_the_report(self):
        """The whole point: this line names the cause, and it was being
        dropped."""
        text = self.report([
            ":irc.example.invalid 366 SomeBot #somechannel :End of /NAMES list.",
            "ERROR :Closing Link: SomeBot[host.invalid] (Max SendQ exceeded)",
        ])

        self.assertIn("Max SendQ exceeded", text)

    def test_every_line_is_shown_in_order(self):
        text = self.report(["first line", "second line", "third line"])
        order = [text.index(x) for x in ("first line", "second line", "third line")]

        self.assertEqual(order, sorted(order))

    def test_it_says_how_many_it_is_showing(self):
        text = self.report(["a", "b", "c"])

        self.assertIn("3 line(s)", text)

    def test_an_empty_ring_says_so_rather_than_printing_a_heading(self):
        """A link that dropped before the server said anything is itself a
        fact worth reading, and a bare heading with nothing under it looks
        like a broken log."""
        text = self.report([])

        self.assertIn("Nothing had been received", text)
        self.assertNotIn("line(s) from the server", text)

    def test_it_never_raises(self):
        """It runs on the way out of a link that has ALREADY failed. A
        logging helper must not be what turns a reconnect into a crash."""
        class Hostile:
            def __len__(self):
                raise RuntimeError("boom")
            def __bool__(self):
                raise RuntimeError("boom")

        irc._report_recent_lines(Hostile())
        irc._report_recent_lines(None)


class TheReadLoopFeedsAndUsesIt(unittest.TestCase):
    """The ring is a local of irc_loop(), which needs a live socket to run,
    so the wiring is checked against the source."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_the_ring_is_bounded(self):
        """Held for the life of a connection on a bot that may never
        disconnect - an unbounded list would grow for months."""
        source = self.source()

        self.assertIn("collections.deque(maxlen=RECENT_LINE_MEMORY)", source)
        self.assertGreater(irc.RECENT_LINE_MEMORY, 1)
        self.assertLessEqual(irc.RECENT_LINE_MEMORY, 100)

    def test_every_inbound_line_is_recorded(self):
        self.assertIn("recent_lines.append(line.strip()[:200])", self.source())

    def test_a_long_line_is_truncated(self):
        """A NAMES burst carries lines of hundreds of nicks. Which commands
        arrived is the diagnostic value, not every name in them."""
        self.assertIn("[:200]", self.source())

    def test_every_disconnect_path_reports(self):
        """Four ways out of the read loop, and the one that mattered was the
        socket error. Missing any of them leaves the same blind spot for a
        different failure."""
        calls = [line.strip() for line in self.source().splitlines()
                 if "_report_recent_lines(recent_lines)" in line
                 and not line.lstrip().startswith("def ")]

        self.assertEqual(len(calls), 4, f"found {calls}")

    def test_a_server_error_line_is_logged_when_it_arrives(self):
        """Not only in the report, and not only under DEBUG_MODE: the server
        explaining itself is never chatter."""
        source = self.source()

        self.assertIn('line.startswith("ERROR ")', source)
        self.assertIn("[SERVER ERROR]", source)

    def test_the_error_log_is_outside_the_debug_filter(self):
        """It used to be reachable only through `if
        getattr(config, 'DEBUG_MODE', False)`, which is off on every install
        that has not gone looking for trouble already."""
        source = self.source()
        error_at = source.index('if line.startswith("ERROR ")')
        debug_at = source.index("if getattr(config, 'DEBUG_MODE', False):\n"
                                "                        is_channel_traffic")

        self.assertLess(error_at, debug_at)

    def test_a_reset_is_no_longer_reported_as_a_dead_network(self):
        """WinError 10054 is ECONNRESET - the server hanging up on us. The
        old wording sent an operator to look at their connection."""
        source = self.source()

        self.assertNotIn("TCP keepalive detected a dead network", source)
        self.assertIn("The link dropped while reading", source)


if __name__ == "__main__":
    unittest.main()
