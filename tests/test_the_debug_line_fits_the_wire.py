"""send_debug() had no line-length budget, unlike every other outbound
builder (audit L30, #694).

The debug PRIVMSG wraps the text in about 170 bytes of colour framing. A
long folder name, a hostmask or an exception's text with an absolute path
pushed the line past the 512 bytes a server relays, and the server cut it
- inside the text, possibly inside a colour code or a multibyte character
- so the debug channel showed a truncated line with the background colour
smeared to the end and the closing block gone. The console sinks and
stdout got the full text; only the channel line was wrong.

The text and the closing block are rendered through fit_irc_line() now,
as the adverts and the notices are: the text is shrunk with an ellipsis
until the whole line fits IRC_LINE_BUDGET, re-rendered from the template on
every attempt so a colour code is never sliced.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheQueuedLine(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(DEBUG_CHANNEL="#dccore-debug", DEBUG_TO_CHANNEL=True, DEBUG_TO_CONSOLE=False)
        announce._debug_queue.clear()
        self.addCleanup(announce._debug_queue.clear)
        # Queued only: the drain thread is not this test's business.
        real = announce._ensure_debug_drain
        announce._ensure_debug_drain = lambda: None
        self.addCleanup(setattr, announce, "_ensure_debug_drain", real)

    def queued(self, text, category="PART"):
        announce.send_debug(text, category=category)
        self.assertEqual(len(announce._debug_queue), 1)
        return announce._debug_queue.popleft()

    def test_the_audits_500_x_fits(self):
        line = self.queued("x" * 500)

        self.assertLessEqual(len(line.encode("utf-8")), announce.IRC_LINE_BUDGET)
        self.assertTrue(line.endswith("\r\n"))

    def test_the_closing_block_is_still_on_the_line(self):
        """What the server's cut lost: the background colour was left open."""
        line = self.queued("Pack denied for someuser: " + "A" * 300 + " is an artist root folder.")

        self.assertLessEqual(len(line.encode("utf-8")), announce.IRC_LINE_BUDGET)
        import theme
        bg_red, bg_cyan, _box, reset = theme.blocks()[:4]
        self.assertTrue(line.endswith(f"{bg_cyan} {bg_red} {reset}\r\n"), repr(line[-40:]))
        self.assertIn("...", line)

    def test_a_multibyte_name_is_measured_in_bytes_and_never_split(self):
        line = self.queued("Pack denied: " + "Ünïcödé–" * 60)

        self.assertLessEqual(len(line.encode("utf-8")), announce.IRC_LINE_BUDGET)
        line.encode("utf-8")  # a split code point would not round-trip
        self.assertIn("...", line)

    def test_a_short_line_is_untouched(self):
        line = self.queued("short and sweet")

        self.assertIn(" Log: short and sweet ", line)
        self.assertNotIn("...", line)

    def test_the_console_still_gets_the_whole_text(self):
        """Only the channel line is bounded; a sink is not a 512-byte wire."""
        self.set_config(DEBUG_TO_CONSOLE=True)
        got = []
        announce.add_debug_sink(lambda text, category: got.append(text))
        self.addCleanup(announce.remove_debug_sink, announce._debug_sinks[-1])

        self.queued("y" * 500)

        self.assertEqual(got, ["y" * 500])


class TheBuilderIsTheSharedOne(unittest.TestCase):

    def test_send_debug_renders_through_fit_irc_line(self):
        import inspect
        body = inspect.getsource(announce.send_debug)

        self.assertIn("msg = fit_irc_line(_build, clean_text)", body)


if __name__ == "__main__":
    unittest.main()
