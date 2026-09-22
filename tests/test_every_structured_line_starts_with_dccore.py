"""Two lines in structured mode did not start with DCCORE: "Goodbye." and
"Line too long." (audit L15, #679).

Session.close(announce_text=...) writes its text inline - the writer thread
is about to stop, so a queued goodbye would never leave - and bypassed
send()'s "DCCORE OUT" wrapping. After `hello`, `quit` and the 4096-byte
guard sent bare lines, against ADMIN-CONSOLE.md's "from then on, every line
it sends on this session starts with DCCORE". dccore.mrc merely echoed
them; a stricter client would have treated them as a protocol error or
routed them as plain chat. close() wraps as send() wraps now; a line that
already is a DCCORE line (DCCORE TAKEN) goes as it is.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_a_taken_over_console_hears_it_and_does_not_take_it_back import make_session, read_all  # noqa: E402


class TheLastLineOfASession(DCCoreTestCase):

    def session(self, structured):
        session, far = make_session("Op", "203.0.113.5", structured=structured)
        self.addCleanup(far.close)
        self.addCleanup(session.close, None)
        return session, far

    def test_quit_says_goodbye_as_a_dccore_line(self):
        """The audit's probe (a)."""
        session, far = self.session(structured=True)

        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_command(session, "quit")

        self.assertEqual(read_all(far), "DCCORE OUT Goodbye.\n")

    def test_the_line_length_guard_too(self):
        """The audit's probe (b)."""
        session, far = self.session(structured=True)

        session.close(announce_text="Line too long.")

        self.assertEqual(read_all(far), "DCCORE OUT Line too long.\n")

    def test_a_line_that_already_is_a_dccore_line_is_not_wrapped_twice(self):
        session, far = self.session(structured=True)

        session.close(announce_text="DCCORE TAKEN 198.51.100.9")

        self.assertEqual(read_all(far), "DCCORE TAKEN 198.51.100.9\n")

    def test_a_plain_session_still_gets_plain_text(self):
        session, far = self.session(structured=False)

        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_command(session, "quit")

        self.assertEqual(read_all(far), "Goodbye.\n")

    def test_nothing_is_sent_for_none(self):
        session, far = self.session(structured=True)

        session.close(announce_text=None)

        self.assertEqual(read_all(far), "")


class TheDocsPromiseHolds(unittest.TestCase):

    def test_the_guide_still_makes_the_promise_close_now_keeps(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            guide = handle.read()

        self.assertIn("every\nline it sends on this session starts with `DCCORE`", guide)


if __name__ == "__main__":
    unittest.main()
