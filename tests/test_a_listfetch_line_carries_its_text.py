"""The LISTFETCH line reached the mIRC window with no text.

A fetched list arrived and the window drew a bare `[LISTS]` tag. The event sink
handed structured_line() only the event's fields, and LISTFETCH's payload is its
sentence, which travels beside the fields, not in them. The tests that shipped
with it built the fields by hand with a `text` in them, so they never saw it.
These go the whole way: announce.feed_event -> the session's event sink -> the
line the client would read.
"""

import os
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import list_fetch  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheWholeWay(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        self.session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        self.session.authenticated = True
        self.session.structured = True
        announce.add_event_sink(self.session.event_sink)
        self.addCleanup(announce.remove_event_sink, self.session.event_sink)

    def lines(self):
        return [line for line in self.session._outbox if line.startswith("DCCORE LISTFETCH")]

    def test_an_arrived_list_carries_its_sentence(self):
        announce.feed_event("LISTFETCH", "SomeBot's list arrived: 110,180 files", bot="SomeBot", action="arrived")
        self.assertEqual(self.lines(), ["DCCORE LISTFETCH SomeBot arrived SomeBot's list arrived: 110,180 files"])

    def test_through_the_helper_the_fetch_code_uses(self):
        list_fetch._tell_the_console("SomeBot", "auto", "SomeBot's list has changed - asking again automatically")
        self.assertEqual(self.lines(),
                         ["DCCORE LISTFETCH SomeBot auto SomeBot's list has changed - asking again automatically"])

    def test_the_text_is_never_empty_on_the_wire(self):
        announce.feed_event("LISTFETCH", "x y z", bot="B", action="unusable")
        self.assertTrue(self.lines()[0].endswith(" x y z"))

    def test_the_fields_still_win_if_an_event_carries_its_own_text(self):
        announce.feed_event("LISTFETCH", "prose", bot="B", action="a", text="from the fields")
        self.assertTrue(self.lines()[0].endswith(" from the fields"))

    def test_the_other_kinds_are_not_changed_by_it(self):
        announce.feed_event("SEARCH", "dave searched", nick="dave", channel="#c", results=3, term="x y")
        self.assertIn("DCCORE SEARCH dave #c 3 x y", list(self.session._outbox))
        announce.feed_event("SENT", "sent", nick="dave", channel="#c", bytes=10, seconds=1.0, bytes_per_s=10, name="f")
        self.assertIn("DCCORE SENT dave #c 10 1.0 10 f", list(self.session._outbox))

    def test_a_plain_session_gets_none_of_it(self):
        self.session.structured = False
        announce.feed_event("LISTFETCH", "t", bot="B", action="auto")
        self.assertEqual(self.lines(), [])


if __name__ == "__main__":
    unittest.main()
