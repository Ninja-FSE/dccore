"""DEBUG_TO_CONSOLE = False did not stop the structured feed's events
(audit L14, #678).

send_debug() honoured DEBUG_TO_CONSOLE before fanning prose out to the
debug sinks; feed_event() handed the fields to the event sinks gated only
by the per-kind tickboxes (console_wants). A structured dccore.mrc session
drops the prose of feed kinds and lives on the fields, so after the
operator unticked "Send debug lines to admin console" the plain console
went quiet as documented while the mIRC window kept showing every
REQUEST/QUEUED/SENDING/SENT/FAIL/SEARCH line - only LOG stopped - and
send_debug()'s stdout floor printed the same event as undelivered at the
same moment. The switch gates the fields now, as it gates the prose.
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
import announce  # noqa: E402

from tests import test_a_structured_feed_for_the_admin_chat as feed  # noqa: E402


class TheSwitchGatesTheFields(feed.FeedEventTellsItTwice):

    def test_with_the_console_off_no_sink_gets_the_event(self):
        self.set_config(DEBUG_TO_CONSOLE=False)

        announce.feed_event("SEARCH", "dave searched x", nick="dave", results=3, term="x")

        self.assertEqual(self.events, [])
        self.assertEqual(self.prose, [("SEARCH", "dave searched x")], "the channel and the floor still get the prose")

    def test_with_it_on_the_sink_gets_it_as_before(self):
        self.set_config(DEBUG_TO_CONSOLE=True)

        announce.feed_event("SEARCH", "dave searched x", nick="dave", results=3, term="x")

        self.assertEqual([k for k, _f, _t in self.events], ["SEARCH"])

    def test_the_tickbox_still_gates_on_its_own(self):
        self.set_config(DEBUG_TO_CONSOLE=True, CONSOLE_SHOW_SEARCHES=False)

        announce.feed_event("SEARCH", "dave searched x", nick="dave", results=3, term="x")

        self.assertEqual(self.events, [])

    def test_the_count_is_still_kept_with_the_console_off(self):
        """What the bot has seen is a fact about the bot (#754), whatever
        reaches a client."""
        import runtime
        before = runtime.feed_counts.get("SEARCH", 0)
        self.set_config(DEBUG_TO_CONSOLE=False)

        announce.feed_event("SEARCH", "dave searched x", nick="dave", results=3, term="x")

        self.assertEqual(runtime.feed_counts.get("SEARCH", 0), before + 1)


for _name in [n for n in dir(feed.FeedEventTellsItTwice) if n.startswith("test")]:
    setattr(TheSwitchGatesTheFields, _name, None)


class AStructuredSession(feed.DCCoreTestCase):
    """The audit's own probe: a real Session in structured mode, its sinks
    attached, the real send_debug in place."""

    def setUp(self):
        super().setUp()
        self.session = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(self.session.close, None)
        self.session.authenticated = True
        self.session.structured = True
        announce.add_debug_sink(self.session.debug_sink)
        self.addCleanup(announce.remove_debug_sink, self.session.debug_sink)
        announce.add_event_sink(self.session.event_sink)
        self.addCleanup(announce.remove_event_sink, self.session.event_sink)
        self.set_config(CONSOLE_SHOW_SEARCHES=True, DEBUG_CHANNEL="", DEBUG_TO_CHANNEL=False)

    def outbox(self):
        return [l for l in self.session._outbox if not l.startswith(("DCCORE STATUS ", "DCCORE SLOT ", "DCCORE QUEUE "))]

    def probe(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            announce.feed_event("SEARCH", "x", nick="n", channel="#c", results=1, term="t")
            announce.send_debug("plain prose", category="INFO")
        return out.getvalue()

    def test_console_on_both_lines_arrive(self):
        self.set_config(DEBUG_TO_CONSOLE=True)

        self.probe()

        self.assertEqual(self.outbox(), ["DCCORE SEARCH n #c 1 t", "DCCORE LOG INFO plain prose"])

    def test_console_off_neither_does(self):
        self.set_config(DEBUG_TO_CONSOLE=False)

        printed = self.probe()

        self.assertEqual(self.outbox(), [])
        # and the floor, which had nowhere to deliver, still says so
        self.assertIn("[DEBUG SEARCH] x", printed)


if __name__ == "__main__":
    unittest.main()
