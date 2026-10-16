"""#1291: reclaim_main_nick(), step 2 of splitting irc_loop().

When the client holding the bot's main nick quits or changes nick and the
bot is on another name, the bot asks for the main nick back - not on a PART,
which frees nothing, and at most once per NICK_RECLAIM_GAP_SECONDS. This was a block inside
irc_loop(), reachable only by driving the whole loop
(tests/test_the_bots_own_nick_follows_the_server.py still does, for the
wiring). Here the function is called directly, for each way it must say no.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class _Socket:
    def __init__(self, fails=False):
        self.sent = []
        self.fails = fails

    def sendall(self, payload):
        if self.fails:
            raise OSError("link down")
        self.sent.append(payload.decode("utf-8").strip())


class TheMainNickIsReclaimedOnlyFromItsHolder(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot_", ALT_NICKNAME="SomeBot_")
        config.ORIGINAL_NICK = "SomeBot"
        self.addCleanup(setattr, config, "ORIGINAL_NICK", "DCCore")

    def reclaim(self, line, sock, now=None):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            asked = irc.reclaim_main_nick(line, sock, now=now)
        return asked, said.getvalue()

    def test_the_holder_quitting_or_renaming_is_answered_with_one_nick(self):
        for line in (":SomeBot!ghost@host.example QUIT :Ping timeout",
                     ":SomeBot!ghost@host.example NICK :SomeoneElse",
                     ":someBOT!ghost@host.example QUIT :case does not matter"):
            with self.subTest(line=line):
                runtime.nick_reclaim_asked_at = None
                sock = _Socket()
                asked, said = self.reclaim(line, sock)
                self.assertTrue(asked)
                self.assertEqual(sock.sent, ["NICK SomeBot"])
                self.assertIn("is free", said)

    def test_asks_are_spaced_however_often_the_holder_leaves(self):
        """A holder that quits and comes straight back, again and again, gets
        one NICK per gap - every one of them unpaced."""
        sock = _Socket()
        gap = irc.NICK_RECLAIM_GAP_SECONDS
        for at in (1000.0, 1001.0, 1000.0 + gap - 1):
            self.reclaim(":SomeBot!ghost@host.example QUIT :bye", sock, now=at)
        self.assertEqual(sock.sent, ["NICK SomeBot"])
        self.reclaim(":SomeBot!ghost@host.example QUIT :bye", sock, now=1000.0 + gap)
        self.assertEqual(sock.sent, ["NICK SomeBot", "NICK SomeBot"])

    def test_a_clock_set_back_does_not_stop_the_asks(self):
        sock = _Socket()
        self.reclaim(":SomeBot!ghost@host.example QUIT :bye", sock, now=5000.0)
        self.reclaim(":SomeBot!ghost@host.example QUIT :bye", sock, now=100.0)
        self.assertEqual(len(sock.sent), 2)

    def test_a_refused_reason_does_not_use_up_the_gap(self):
        """Somebody else leaving is no ask, so it must not delay the real one."""
        sock = _Socket()
        self.reclaim(":SomeBot2!x@host.example QUIT :bye", sock, now=1000.0)
        self.reclaim(":SomeBot!ghost@host.example QUIT :bye", sock, now=1001.0)
        self.assertEqual(sock.sent, ["NICK SomeBot"])

    def test_the_name_is_asked_for_not_taken(self):
        self.reclaim(":SomeBot!ghost@host.example QUIT :Ping timeout", _Socket())
        self.assertEqual(config.NICKNAME, "SomeBot_", "the server's NICK event renames the bot (#635), not the ask")

    def test_a_bot_already_on_its_main_nick_does_not_ask(self):
        config.NICKNAME = "somebot"
        sock = _Socket()
        self.assertEqual(self.reclaim(":SomeBot!other@host.example QUIT :bye", sock), (False, ""))
        self.assertEqual(sock.sent, [])

    def test_somebody_else_leaving_is_not_a_reason(self):
        sock = _Socket()
        self.assertEqual(self.reclaim(":SomeBot2!x@host.example QUIT :bye", sock), (False, ""))
        self.assertEqual(sock.sent, [])

    def test_a_quit_reason_naming_the_main_nick_is_not_a_reason(self):
        sock = _Socket()
        for line in (":attacker!u@host.example QUIT :bye :SomeBot!x@y",
                     ":attacker!u@host.example PART #somechannel :SomeBot"):
            with self.subTest(line=line):
                self.assertEqual(self.reclaim(line, sock), (False, ""))
        self.assertEqual(sock.sent, [])

    def test_a_failed_send_is_logged_and_does_not_escape(self):
        asked, said = self.reclaim(":SomeBot!ghost@host.example QUIT :Ping timeout", _Socket(fails=True))
        self.assertTrue(asked)
        self.assertIn("[NICK RECOVERY ERROR] Could not reclaim the nick: link down", said)


if __name__ == "__main__":
    unittest.main()
