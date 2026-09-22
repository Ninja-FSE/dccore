"""#594: the bot never adopted the nick the server shortened it to.

A nickname longer than the server's NICKLEN is not refused, it is shortened
("DCCore-Server" registers as "DCCore-Serve" on Undernet). The code that adopts
the server's own name from the 001 it addresses to us sat in the pre-registration
loop - which sends USER and stops at the first NOTICE/PING, and 001 is only sent
AFTER USER, so it could never run. 001 always arrived in the main read loop,
which set `joined` and never read its target. config.NICKNAME stayed the long
name: every `target_chan.lower() == config.NICKNAME.lower()` test failed for
private messages to the real nick (the DCC CHAT console, cross-bot DCC SEND
offers, DCC RESUME, broadcast-search replies), a KICK of the bot went
unrecognised, the advert published a nick nobody can PM, and the 005 explanation
printed "so it was shortened to <the long name>".

irc_loop() is one monolithic function that needs a live socket, so, like the
other tests of it, the wiring is read from the source; the rule is a plain
function and is run.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

WELCOME = ":irc.undernet.example 001 DCCore-Serve :Welcome to the Undernet IRC Network DCCore-Serve"


class TheRule(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="DCCore-Server", ORIGINAL_NICK="DCCore-Server", PREVIOUS_NICK=None)

    def test_a_shortened_nick_is_adopted(self):
        with mock.patch("builtins.print"):
            adopted = irc.adopt_registered_nick(WELCOME)
        self.assertEqual(adopted, "DCCore-Serve")
        self.assertEqual(config.NICKNAME, "DCCore-Serve")

    def test_the_configured_name_is_remembered(self):
        with mock.patch("builtins.print"):
            irc.adopt_registered_nick(WELCOME)
        self.assertEqual(config.PREVIOUS_NICK, "DCCore-Server")
        self.assertEqual(config.ORIGINAL_NICK, "DCCore-Server", "ORIGINAL_NICK keeps the configured value")

    def test_the_bot_answers_to_both_names_afterwards(self):
        with mock.patch("builtins.print"):
            irc.adopt_registered_nick(WELCOME)
        aliases = [alias.lower() for alias in irc.get_bot_aliases()]
        self.assertIn("dccore-serve", aliases)
        self.assertIn("dccore-server", aliases)

    def test_the_same_name_changes_nothing_and_says_nothing(self):
        self.set_config(NICKNAME="DCCore-Serve")
        with mock.patch("builtins.print") as said:
            self.assertIsNone(irc.adopt_registered_nick(WELCOME))
        said.assert_not_called()
        self.assertEqual(config.NICKNAME, "DCCore-Serve")

    def test_a_star_target_is_not_a_nick(self):
        self.assertIsNone(irc.adopt_registered_nick(":irc.example 001 * :Welcome"))
        self.assertEqual(config.NICKNAME, "DCCore-Server")

    def test_a_line_that_is_not_a_numeric_is_ignored(self):
        self.assertIsNone(irc.adopt_registered_nick("PING :irc.example"))
        self.assertEqual(config.NICKNAME, "DCCore-Server")

    def test_it_says_what_it_did(self):
        with mock.patch("builtins.print") as said:
            irc.adopt_registered_nick(WELCOME)
        text = " ".join(str(call.args[0]) for call in said.call_args_list)
        self.assertIn("Registered as DCCore-Serve, not DCCore-Server", text)


class TheWiring(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            self.source = handle.read()
        self.loop = self.source[self.source.index("def irc_loop():"):]

    def test_it_is_read_where_001_actually_arrives(self):
        start = self.loop.index('if not joined and (is_server_numeric(line, "001") or is_server_numeric(line, "376")):')
        block = self.loop[start:start + 400]
        self.assertIn('if is_server_numeric(line, "001"):', block)
        self.assertIn("adopt_registered_nick(line)", block)
        self.assertLess(block.index("adopt_registered_nick(line)"), block.index("joined = True"))

    def test_the_dead_copy_before_registration_is_gone(self):
        """001 cannot arrive before USER is sent; the block there never ran.
        Since #634 nothing is read before USER goes out at all - NICK and
        USER are sent back to back and the main loop is the registration -
        so the pre-registration reader this block sat in is gone with it."""
        prereg = self.loop[:self.loop.index("s.settimeout(20.0)")]
        self.assertNotIn("numeric_target(a_line)", prereg)
        self.assertNotIn('is_server_numeric(a_line, "001")', prereg)
        self.assertNotIn("s.recv(", prereg, "a line is read before USER is sent")

    def test_the_explanation_names_the_adopted_nick(self):
        """It sits earlier in the source than the 001 handling but runs after
        it: 001 is the first numeric a server sends, 005 comes later. It prints
        config.NICKNAME, which is the adopted name by then."""
        self.assertIn("shortened to {config.NICKNAME}", self.loop)
        self.assertIn('is_server_numeric(line, "005")', self.loop)

    def test_the_helper_is_the_only_place_that_takes_the_servers_name(self):
        lines = [l for l in self.source.splitlines() if "config.NICKNAME = given" in l]
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
