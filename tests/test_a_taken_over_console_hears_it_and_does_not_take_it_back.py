"""#583, #597, #600: the console that was replaced never heard that it had been.

_promote() queued "Session taken over from ..." on the old session's outbox and
closed its socket on the very next statement, so the writer thread found the
session closed before it sent a byte (0 of 40 deliveries over loopback). And in
structured mode the line would have been wrapped as `DCCORE OUT ...`, which the
script routes to its console echo without ever reaching its "taken" check.
The replaced script therefore stayed in state `in`, reconnected five seconds
later with its stored token and took the console straight back; two scripts
traded it for ever, contrary to what docs/ADMIN-CONSOLE.md promises.

Now the notice is written inline (close(announce_text=...)), and a structured
session gets a line of its own, `DCCORE TAKEN <ip>`, which the script turns into
its `taken` state - the state in which it does not reconnect by itself.
"""

import io
import os
import re
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def make_session(nick, peer_ip, structured=False):
    """A real Session over a socket pair; returns (session, the far end)."""
    near, far = socket.socketpair()
    far.settimeout(5)
    session = adminchat.Session(near, peer_ip, nick, f"{nick}.users.undernet.org")
    session.authenticated = True
    session.structured = structured
    return session, far


def read_all(sock):
    chunks = []
    while True:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode("utf-8", "replace")


class TheReplacedConsole(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, adminchat, "_session", adminchat._session)
        self.addCleanup(setattr, adminchat, "_pending", adminchat._pending)
        adminchat._session = None
        adminchat._pending = None

    def take_over(self, structured):
        old, old_far = make_session("Op", "203.0.113.5", structured=structured)
        new, new_far = make_session("Op", "198.51.100.9")
        for sock in (old_far, new_far):
            self.addCleanup(sock.close)
        self.addCleanup(old.close, None)
        self.addCleanup(new.close, None)
        adminchat._session = old
        replaced = adminchat._promote(new)
        self.assertIs(replaced, old)
        return old, old_far, new

    def test_a_plain_session_is_told_before_its_socket_closes(self):
        old, old_far, _new = self.take_over(structured=False)
        received = read_all(old_far)
        self.assertEqual(received, "Session taken over from 198.51.100.9. Closing this one.\n")

    def test_a_structured_session_gets_a_line_of_its_own(self):
        old, old_far, _new = self.take_over(structured=True)
        received = read_all(old_far)
        self.assertEqual(received, "DCCORE TAKEN 198.51.100.9\n")
        self.assertNotIn("DCCORE OUT", received, "the notice must not look like a command's reply")

    def test_the_old_session_is_closed_and_the_new_one_is_current(self):
        old, _far, new = self.take_over(structured=True)
        self.assertTrue(old.closed)
        self.assertIs(adminchat._session, new)

    def test_a_first_login_tells_nobody(self):
        new, new_far = make_session("Op", "198.51.100.9")
        self.addCleanup(new_far.close)
        self.addCleanup(new.close, None)
        self.assertIsNone(adminchat._promote(new))
        self.assertEqual(read_all(new_far), "")

    def test_promoting_the_current_session_again_closes_nothing(self):
        new, new_far = make_session("Op", "198.51.100.9")
        self.addCleanup(new_far.close)
        self.addCleanup(new.close, None)
        adminchat._session = new
        adminchat._promote(new)
        self.assertFalse(new.closed)


class TheScript(unittest.TestCase):

    def text(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
            return handle.read().replace("\r\n", "\n")

    def test_it_handles_the_taken_line_by_entering_the_taken_state(self):
        text = self.text()
        start = text.index("if (%type == TAKEN) {")
        block = text[start:text.index("\n  }", start)]
        self.assertIn("hadd dccore.live state taken", block)

    def test_the_taken_state_is_the_one_that_does_not_reconnect(self):
        text = self.text()
        block = text[text.index("if (%was == taken) {"):]
        block = block[:block.index("return")]
        self.assertIn("Not reconnecting by itself", block)

    def test_the_plain_notice_is_still_recognised(self):
        self.assertIn("if (Session taken over from * iswm %text) {", self.text())

    def test_the_line_is_in_the_protocol_table(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            self.assertRegex(handle.read(), r"\| `DCCORE TAKEN <ip>` \|")


if __name__ == "__main__":
    unittest.main()
