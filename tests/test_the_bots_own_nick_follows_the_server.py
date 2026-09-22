"""config.NICKNAME was set at every NICK send and never reconciled (audit
M33, #635).

Three paths renamed the bot after registration - the reclaim of the main
nick when its holder quit, the background monitor that does the same on a
timer, and a rehash that found NICKNAME changed in the file - and all three
assigned config.NICKNAME the moment they wrote the NICK command, before the
server had answered. Nothing put it back. The NICK event the server sends
for a rename that WORKED was only used to move the renamed user's queue;
one for the bot itself was not recognised as such, and a refusal - 438
"nick change too fast" within Undernet's 30 s window, 433 for a name
somebody took meanwhile, a rename services forced on the bot - left the
server knowing the bot by one name and config.NICKNAME saying another until
the next reconnect. Every "is this for me" test compared against a nick the
bot did not hold: DCC CHAT, SEND and RESUME offers, private messages, the
self-message filter, a KICK of the bot.

The senders no longer assign. Once registered, the server's own NICK event
for the name the bot holds is the one thing that changes config.NICKNAME,
and a refused NICK means the bot keeps the name it had. The registration
itself is unchanged: the ladder (#633) still assigns while unregistered,
because the 001 settles it (adopt_registered_nick()).
"""

import io
import os
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_a_third_nick_when_both_are_taken import DrivesOneRegistration, server  # noqa: E402

NOTICE_AUTH = ":irc.example.net NOTICE AUTH :*** Looking up your hostname"


def welcome(nick):
    return ":irc.example.net 001 %s :Welcome to the network, %s" % (nick, nick)


def renamed(old, new):
    return ":%s!bot@host NICK :%s" % (old, new)


class TheServersNickEventIsTheOnlyRename(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot_", ORIGINAL_NICK="SomeBot")

    def test_our_own_rename_is_followed(self):
        self.assertTrue(irc.note_own_nick_change("SomeBot_", "SomeBot"))

        self.assertEqual(config.NICKNAME, "SomeBot")

    def test_a_forced_rename_is_followed_too(self):
        """Services renamed the bot to something it never asked for. The
        server is right about what it calls us."""
        irc.note_own_nick_change("somebot_", "Guest12345")

        self.assertEqual(config.NICKNAME, "Guest12345")

    def test_somebody_elses_rename_is_not(self):
        self.assertFalse(irc.note_own_nick_change("somebody", "SomeBot"))
        self.assertFalse(irc.note_own_nick_change("SomeBot", "SomeBot2"),
                         "the MAIN nick is not ours while we hold the alternate")

        self.assertEqual(config.NICKNAME, "SomeBot_")

    def test_a_rename_to_the_name_we_already_hold_is_nothing(self):
        self.assertFalse(irc.note_own_nick_change("SomeBot_", "SomeBot_"))


class _NeverStarts:
    """threading.Thread for the read loop: the JOIN, the watchdog and the
    advert worker it would start after 001 are not this test's business."""

    def __init__(self, *_a, **_k):
        pass

    def start(self):
        pass


class DrivesPastRegistration(DrivesOneRegistration):
    """The ladder harness, with the threads irc_loop() starts after 001
    stubbed out so a rename can be scripted on a registered bot."""

    def setUp(self):
        super().setUp()
        stub = types.ModuleType("threading")
        stub.__dict__.update(threading.__dict__)
        stub.Thread = _NeverStarts
        self._real_threading = irc.threading
        irc.threading = stub
        self.addCleanup(setattr, irc, "threading", self._real_threading)


class ARegisteredBotIsRenamedOnlyByTheServer(DrivesPastRegistration):

    def test_a_forced_rename_changes_what_the_bot_answers_to(self):
        """The audit's S1: 001, then the server renames the bot. The old
        handler moved presence and left config.NICKNAME alone."""
        self.run_registration(NOTICE_AUTH, welcome("SomeBot"), renamed("SomeBot", "Guest12345"))

        self.assertEqual(config.NICKNAME, "Guest12345")

    def test_a_reclaim_is_not_the_bots_name_until_the_server_says_so(self):
        """Registered as the alternate; the main nick's holder quits; the bot
        asks for it back. Between the ask and the answer it is still the
        alternate."""
        sent = self.run_registration(
            NOTICE_AUTH, server("433", "SomeBot"), welcome("SomeBot_"),
            ":SomeBot!ghost@host QUIT :Ping timeout")

        self.assertIn("NICK SomeBot", sent[2:], "the reclaim was not asked for")
        self.assertEqual(config.NICKNAME, "SomeBot_")

    def test_and_becomes_it_when_the_server_answers(self):
        self.run_registration(
            NOTICE_AUTH, server("433", "SomeBot"), welcome("SomeBot_"),
            ":SomeBot!ghost@host QUIT :Ping timeout",
            renamed("SomeBot_", "SomeBot"))

        self.assertEqual(config.NICKNAME, "SomeBot")

    def test_a_reclaim_refused_as_too_fast_leaves_the_name_alone(self):
        """Undernet's 438. The old code had already written the main nick
        into config at the send, so every private-target check was wrong
        from here until the next reconnect."""
        sent = self.run_registration(
            NOTICE_AUTH, server("433", "SomeBot"), welcome("SomeBot_"),
            ":SomeBot!ghost@host QUIT :Ping timeout",
            ":irc.example.net 438 SomeBot_ SomeBot :Nick change too fast. Please wait 30 seconds.")

        self.assertEqual(config.NICKNAME, "SomeBot_")
        nicks = [line for line in sent if line.startswith("NICK ")]
        self.assertEqual(nicks, ["NICK SomeBot", "NICK SomeBot_", "NICK SomeBot"],
                         "a refused reclaim must not be answered with another NICK")

    def test_a_reclaim_refused_as_taken_leaves_the_name_alone(self):
        """433 after registration: somebody else took the main nick in the
        gap. Not the registration ladder - the bot has a name."""
        sent = self.run_registration(
            NOTICE_AUTH, server("433", "SomeBot"), welcome("SomeBot_"),
            ":SomeBot!ghost@host QUIT :Ping timeout",
            server("433", "SomeBot", target="SomeBot_"))

        self.assertEqual(config.NICKNAME, "SomeBot_")
        self.assertNotIn("NICK SomeBot_1", sent)
        self.assertEqual(sent.count("NICK SomeBot_"), 1, "the alternate was re-sent to a server that already calls us that")


class TheSendersNoLongerAssign(unittest.TestCase):
    """The text: after registration the only assignment to config.NICKNAME
    in irc.py is the server's NICK event, and the rehash rename keeps the
    live name until the server confirms the new one."""

    def source(self, name):
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_only_the_registration_paths_assign_in_the_read_loop(self):
        loop = self.source("irc.py").split("def irc_loop():", 1)[1]
        assigning = [line.strip() for line in loop.splitlines()
                     if line.strip().startswith("config.NICKNAME = ")]

        self.assertEqual(assigning, [
            "config.NICKNAME = config.ORIGINAL_NICK",   # every connection starts over
            "config.NICKNAME = next_nick",              # the ladder, before the 001
        ])

    def test_the_rehash_rename_asks_and_keeps_the_live_name(self):
        rehash = self.source("commands.py")
        branch = rehash.split("_cfg.ORIGINAL_NICK = _cfg.NICKNAME", 1)[1][:2500]

        self.assertIn("_wanted_nick = _cfg.NICKNAME", branch)
        self.assertIn("_cfg.NICKNAME = live_nick", branch)
        self.assertIn("rehash_nick_change_line(baseline_nick, _wanted_nick)", branch)


if __name__ == "__main__":
    unittest.main()
