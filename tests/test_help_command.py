"""@<nick>-help - how a stranger finds out what this bot is.

Somebody sees the advert in a busy channel. The advert has one line and no
room to explain anything beyond the trigger, and until now there was no way to
ask. Every other serving script on the network answers a help request.

The two things that can go wrong are not the wording:

  * telling somebody to type a command this bot will refuse - !rar when
    folder packing is off, which is the same mistake the album list was
    making before #153;
  * answering in the channel, which turns one person's question into
    everybody's problem.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class HelpCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="DCCoreWin", RAR_ENABLED=True)

    def ask(self, user="dave", target="#dccore-test"):
        self.oserve.queued.clear()
        commands.handle_help_request(None, user, target)
        return [message for _user, message, _vip in self.oserve.queued]

    def bodies(self, lines):
        """Just the text, without the NOTICE envelope or the colour codes."""
        out = []
        for line in lines:
            body = line.split(":", 1)[1] if ":" in line else line
            for code in (config.C_BOLD, config.C_RED, config.C_RESET):
                body = body.replace(code, "")
            out.append(body.strip())
        return out


class ItAnswersThePersonNotTheChannel(HelpCase):
    """A stranger working out how the bot works must not cost the channel
    anything - which is the whole reason this is worth having rather than
    people asking in the room."""

    def test_every_line_is_a_notice_to_the_asker(self):
        for line in self.ask(user="dave"):
            with self.subTest(line=line[:40]):
                self.assertTrue(line.startswith("NOTICE dave :"), line)

    def test_nothing_is_sent_to_the_channel(self):
        for line in self.ask(target="#dccore-test"):
            with self.subTest(line=line[:40]):
                self.assertNotIn("#dccore-test", line)

    def test_it_is_addressed_to_whoever_asked(self):
        self.assertTrue(all(line.startswith("NOTICE someoneelse :")
                            for line in self.ask(user="someoneelse")))

    def test_it_goes_out_on_the_vip_lane(self):
        """Same lane as the other user commands: an answer to a direct
        question should not queue behind a channel advert."""
        self.ask()

        self.assertTrue(all(vip for _user, _message, vip in self.oserve.queued))


class ItOnlySuggestsWhatTheBotWillDo(HelpCase):

    def test_it_explains_the_list_and_a_file_request(self):
        bodies = " ".join(self.bodies(self.ask()))

        self.assertIn("@DCCoreWin", bodies)
        self.assertIn("!DCCoreWin", bodies)

    def test_it_explains_the_album_request_when_folder_packing_is_on(self):
        self.assertIn("!rar", " ".join(self.bodies(self.ask())))

    def test_it_says_nothing_about_rar_when_folder_packing_is_off(self):
        """The same mistake the album list was making before #153: handing
        somebody an instruction this bot answers with "Folder packing (!rar)
        is disabled on this bot"."""
        self.set_config(RAR_ENABLED=False)

        self.assertNotIn("!rar", " ".join(self.bodies(self.ask())))

    def test_the_rest_of_the_help_survives_with_folder_packing_off(self):
        """Only the album line goes. A bot with !rar off is still a file
        server, and a newcomer still needs to be told how to use it."""
        self.set_config(RAR_ENABLED=False)
        bodies = " ".join(self.bodies(self.ask()))

        self.assertIn("@DCCoreWin", bodies)
        self.assertIn("!DCCoreWin", bodies)
        self.assertIn("-que", bodies)

    def test_it_names_the_queue_commands_that_exist(self):
        """-que and -remove are real; anything else listed here would be a
        newcomer's first experience of the bot ignoring them."""
        bodies = " ".join(self.bodies(self.ask()))

        self.assertIn("@DCCoreWin-que", bodies)
        self.assertIn("@DCCoreWin-remove", bodies)

    def test_it_uses_the_configured_nick_not_a_hardcoded_one(self):
        self.set_config(NICKNAME="SomeOtherBot")
        bodies = " ".join(self.bodies(self.ask()))

        self.assertIn("@SomeOtherBot", bodies)
        self.assertNotIn("DCCoreWin", bodies)


class ItFitsOnTheWire(HelpCase):
    """IRC drops anything past 512 bytes including the prefix the server adds.
    announce.py keeps a 420-byte budget for that; one long help message would
    sit on that edge and lose its tail on the nicks with the longest
    hostmasks - the least predictable way to break."""

    def test_no_line_comes_near_the_budget(self):
        for line in self.ask():
            with self.subTest(line=line[:40]):
                self.assertLess(len(line.encode("utf-8")), announce.IRC_LINE_BUDGET)

    def test_a_long_nickname_still_fits(self):
        """NICKLEN is 12 on Undernet and MAXNICKLEN 15, and the colour codes
        repeat the nick several times per line."""
        self.set_config(NICKNAME="A" * 15)

        for line in self.ask(user="B" * 15):
            with self.subTest(line=line[:40]):
                self.assertLess(len(line.encode("utf-8")), announce.IRC_LINE_BUDGET)


class ItIsReachable(unittest.TestCase):
    """A handler nothing dispatches answers nobody - the #119 shape."""

    def source(self, name):
        with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_channel_dispatch_calls_it(self):
        calls = [line.strip() for line in self.source("irc.py").splitlines()
                 if "handle_help_request" in line and not line.strip().startswith("#")]

        self.assertTrue(calls, "nothing routes @<nick>-help, so asking does nothing")

    def test_it_is_metered_like_the_other_user_commands(self):
        """-help is sent by strangers, so it goes in the anti-flood budget
        beside -que and -remove. Left out, it would be the one user command
        somebody could repeat without limit."""
        source = self.source("irc.py")
        block = source[source.index("is_bot_command = ("):]
        block = block[:block.index(")\n")]

        self.assertIn("-help", block)
        self.assertIn("-que", block, "the block being checked is the wrong one")


if __name__ == "__main__":
    unittest.main()
