"""Phase 3: where announce.send_debug's output actually goes.

Two switches and a floor. The switches are the operator's choice; the floor is
not optional, because the case worth protecting is a daemon in trouble with the
channel turned off and nobody connected to the console. A line that reaches
neither must still reach stdout, or it never existed.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import announce  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

MARKER = "routing-test-marker"


class DebugRouting(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.received = []
        self._sinks = announce._debug_sinks[:]
        announce._debug_sinks.clear()
        self.addCleanup(lambda: (announce._debug_sinks.clear(),
                                 announce._debug_sinks.extend(self._sinks)))

        announce._debug_queue.clear()
        self.addCleanup(announce._debug_queue.clear)

        for name in ("DEBUG_TO_CHANNEL", "DEBUG_TO_CONSOLE"):
            original = getattr(config, name, True)
            self.addCleanup(lambda n=name, v=original: setattr(config, n, v))

        # The drain thread would empty the queue underneath the assertions, so
        # it is kept from starting; what matters here is what was ENQUEUED.
        self._drain = announce._debug_drain_started
        announce._debug_drain_started = True
        self.addCleanup(lambda: setattr(announce, "_debug_drain_started", self._drain))

    def attach_console(self):
        announce.add_debug_sink(
            lambda text, category: self.received.append((category, text)))

    def emit(self):
        """Send one line, capturing anything that lands on stdout."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            announce.send_debug(MARKER, category="INFO")
        return buffer.getvalue()

    def channel_lines(self):
        return [line for line in announce._debug_queue if MARKER in line]

    # -- defaults ----------------------------------------------------------

    def test_both_switches_default_on(self):
        """Nobody who ignores this feature should notice it exists."""
        self.assertIs(getattr(config, "DEBUG_TO_CHANNEL", None), True)
        self.assertIs(getattr(config, "DEBUG_TO_CONSOLE", None), True)

    def test_by_default_the_line_goes_to_both(self):
        self.attach_console()
        self.emit()
        self.assertEqual(len(self.channel_lines()), 1)
        self.assertEqual(self.received, [("INFO", MARKER)])

    # -- the channel switch ------------------------------------------------

    def test_the_channel_can_be_switched_off(self):
        """The point of the console: stop publishing internals to a channel."""
        self.attach_console()
        config.DEBUG_TO_CHANNEL = False
        self.emit()
        self.assertEqual(self.channel_lines(), [])
        self.assertEqual(self.received, [("INFO", MARKER)],
                         "the console must still get it")

    def test_the_console_can_be_switched_off(self):
        self.attach_console()
        config.DEBUG_TO_CONSOLE = False
        self.emit()
        self.assertEqual(len(self.channel_lines()), 1)
        self.assertEqual(self.received, [])

    # -- the floor ---------------------------------------------------------

    def test_nothing_is_lost_when_both_are_off(self):
        config.DEBUG_TO_CHANNEL = False
        config.DEBUG_TO_CONSOLE = False
        printed = self.emit()
        self.assertIn(MARKER, printed,
                      "a line nobody took must still reach stdout")
        self.assertIn("INFO", printed)

    def test_nothing_is_lost_when_the_channel_is_off_and_no_console_is_attached(self):
        """The case that matters: something breaks while nobody is connected."""
        config.DEBUG_TO_CHANNEL = False
        config.DEBUG_TO_CONSOLE = True      # enabled, but nothing registered
        printed = self.emit()
        self.assertIn(MARKER, printed)

    def test_the_floor_does_not_double_log_in_normal_operation(self):
        """44 call sites already print; the floor must not add noise to them."""
        self.attach_console()
        printed = self.emit()
        self.assertNotIn(MARKER, printed,
                         "stdout is the fallback, not a third destination")

    def test_a_console_alone_is_enough_to_suppress_the_floor(self):
        self.attach_console()
        config.DEBUG_TO_CHANNEL = False
        printed = self.emit()
        self.assertNotIn(MARKER, printed)
        self.assertEqual(self.received, [("INFO", MARKER)])

    def test_the_channel_alone_is_enough_to_suppress_the_floor(self):
        config.DEBUG_TO_CONSOLE = False
        printed = self.emit()
        self.assertNotIn(MARKER, printed)
        self.assertEqual(len(self.channel_lines()), 1)

    # -- the delivery count the floor depends on ---------------------------

    def test_fan_out_reports_how_many_sinks_took_the_line(self):
        self.assertEqual(announce._fan_out_to_sinks("x", "INFO"), 0)
        self.attach_console()
        self.assertEqual(announce._fan_out_to_sinks("x", "INFO"), 1)

    def test_a_raising_sink_does_not_count_as_delivery(self):
        """Otherwise a broken console would silently suppress the floor."""
        def bad(text, category):
            raise RuntimeError("sink is broken")
        announce.add_debug_sink(bad)
        self.assertEqual(announce._fan_out_to_sinks("x", "INFO"), 0)

        config.DEBUG_TO_CHANNEL = False
        printed = self.emit()
        self.assertIn(MARKER, printed,
                      "a sink that threw did not take the line, so stdout must")


if __name__ == "__main__":
    unittest.main()
