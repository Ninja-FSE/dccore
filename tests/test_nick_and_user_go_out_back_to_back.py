"""USER was only sent after the server had spoken (audit M32, #634).

After NICK, the handshake waited for a line containing 001, 002, PING or
NOTICE before it sent USER. A server that says nothing until it has both
lines - some ircds, most bouncers - never triggered it: the 70 s recv timed
out and the connect was retried every 80 s for ever, with a log that only
said "timed out" and nothing saying USER had never gone out. And when the
trigger did arrive, the reader broke out of the chunk it was in: every line
already decoded after it was dropped, and the partial line left in its
buffer with them. A 433 sharing a recv() chunk with "NOTICE AUTH" (fast DNS,
slow client) was thrown away and the bot waited for a registration the
server had already refused.

NICK and USER now go out back to back, as every other client sends them,
and the main loop - which answers PING, walks the nick ladder (#633) and
adopts the name from the 001 - is the registration from the first byte.
Driven for real against a scripted socket, like the ladder tests.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402

from tests.test_a_third_nick_when_both_are_taken import DrivesOneRegistration, server  # noqa: E402

NOTICE_AUTH = ":irc.example.net NOTICE AUTH :*** Looking up your hostname"


class TheRegistrationDoesNotWaitToBeSpokenTo(DrivesOneRegistration):

    def test_a_silent_server_still_gets_both_lines(self):
        """The link breaks before the server has said a word. NICK and USER
        were both already on the wire - the old handshake had sent NICK and
        was still waiting for permission to send USER."""
        sent = self.run_registration()

        self.assertEqual(sent[0], "NICK SomeBot")
        self.assertTrue(sent[1].startswith("USER "), sent)

    def test_nick_first_then_user_and_nothing_in_between(self):
        sent = self.run_registration(NOTICE_AUTH)

        self.assertEqual([line.split()[0] for line in sent[:2]], ["NICK", "USER"])

    def test_the_user_line_carries_the_registration_names(self):
        ident, real = irc.registration_names()
        sent = self.run_registration()

        self.assertEqual(sent[1], "USER %s 0 * :%s" % (ident, real))

    def test_a_refusal_sharing_a_chunk_with_the_first_notice_is_not_lost(self):
        """The audit's second finding: both lines in ONE recv() chunk. The
        old reader broke out at the NOTICE and never saw the 433."""
        chunk = (NOTICE_AUTH + "\r\n" + server("433", "SomeBot") + "\r\n").encode("utf-8")
        nicks = self.nicks_asked_for(chunk)

        self.assertEqual(nicks, ["NICK SomeBot", "NICK SomeBot_"])

    def test_a_refusal_split_across_two_chunks_is_not_lost_either(self):
        """The partial line after the trigger used to be discarded with the
        buffer that held it."""
        whole = NOTICE_AUTH + "\r\n" + server("433", "SomeBot") + "\r\n"
        cut = len(NOTICE_AUTH) + 2 + 20
        nicks = self.nicks_asked_for(whole[:cut].encode("utf-8"), whole[cut:].encode("utf-8"))

        self.assertEqual(nicks, ["NICK SomeBot", "NICK SomeBot_"])

    def test_a_ping_before_registration_is_answered(self):
        """Some servers PING before 001 to prove the client is real. The old
        handshake used the PING only as its cue to send USER and never
        answered it; the main loop does."""
        sent = self.run_registration("PING :4A2B3C")

        self.assertIn("PONG 4A2B3C", sent)


class NothingIsReadBeforeUserIsSent(unittest.TestCase):
    """The shape, in the text: between connect() and the reader's own
    recv() there is no recv() and no loop - the two lines just go out."""

    def between_connect_and_the_reader(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()
        loop = source[source.index("def irc_loop():"):]
        start = loop.index("s.connect((config.SERVER, config.PORT))")
        end = loop.index("s.settimeout(20.0)")
        return loop[start:end]

    def test_no_line_is_read_first(self):
        self.assertNotIn("s.recv(", self.between_connect_and_the_reader())

    def test_the_user_line_is_sent_right_after_nick(self):
        block = self.between_connect_and_the_reader()
        nick_at = block.index('s.sendall(f"NICK {config.NICKNAME}')
        user_at = block.index('s.sendall(f"USER {ident_str} 0 * :{real_str}')

        self.assertLess(nick_at, user_at)
        self.assertNotIn("while ", block[nick_at:user_at])


if __name__ == "__main__":
    unittest.main()
