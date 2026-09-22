"""irc_loop's inline dispatch - the admin gates on !ping and !debugnames
and the CTCP VERSION reply - had only source-substring tests (audit L39,
#703).

The text tests catch a gate moved after its action, but a gate kept in
place and neutralised (`... and False:`) passed all nine of them, and the
verifier proved it with a mutant. !ping has an executed backstop of its
own inside handle_ping_request(); !debugnames does not - its RAM-CHECK
notice is built and queued inline in the loop - and VERSION's reply is
queued inline too.

Since #789 irc_loop() is driven for real against a scripted server, so
the gates can be executed rather than read: a channel line from a stranger
and from the admin, after 001, with the loop's threads recorded and the
fake oserve's queue watched. The audit's mutant fails here.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import irc  # noqa: E402

from tests import test_the_bots_own_nick_follows_the_server as own  # noqa: E402
from tests.test_what_a_user_typed_reaches_the_log_printable import _Records  # noqa: E402

CHANNEL = "#somechannel"


class TheGatesInTheLoop(own.DrivesPastRegistration):

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_NICK="TheOperator", ADMIN_HOSTMASKS=[], CTCP_VERSION_REPLY=True)
        _Records.started = []
        irc.threading.Thread = _Records

    def channel_line(self, nick, text):
        return ":%s!~u@%s.example PRIVMSG %s :%s" % (nick, nick.lower(), CHANNEL, text)

    def after_001(self, *lines):
        self.oserve.queued[:] = []
        self.run_registration(own.NOTICE_AUTH, own.welcome("SomeBot"), *lines)

    def queued_notices(self):
        return [m for _u, m, *_ in self.oserve.queued if m.startswith("NOTICE ")]

    # --- !debugnames: the branch with no executed backstop ---------------

    def test_a_strangers_debugnames_queues_nothing(self):
        self.after_001(self.channel_line("stranger", "!debugnames"))

        self.assertEqual([m for m in self.queued_notices() if "RAM-CHECK" in m], [])

    def test_the_admins_debugnames_gets_the_ram_check(self):
        self.after_001(self.channel_line("TheOperator", "!debugnames"))

        checks = [m for m in self.queued_notices() if "RAM-CHECK" in m]
        self.assertEqual(len(checks), 1, self.oserve.queued)
        self.assertTrue(checks[0].startswith("NOTICE TheOperator :[RAM-CHECK]"))

    # --- !ping ------------------------------------------------------------

    def test_a_strangers_ping_starts_no_thread(self):
        self.after_001(self.channel_line("stranger", "!ping"))

        self.assertEqual([t for t, _a in _Records.started if t is commands.handle_ping_request], [])

    def test_the_admins_ping_does(self):
        self.after_001(self.channel_line("TheOperator", "!ping"))

        pings = [a for t, a in _Records.started if t is commands.handle_ping_request]
        self.assertEqual([a[1:] for a in pings], [("TheOperator", CHANNEL)])

    # --- CTCP VERSION: queued VIP, not written to the socket ----------------

    def test_a_ctcp_version_is_answered_through_the_paced_queue(self):
        self.after_001(":asker!~u@asker.example PRIVMSG SomeBot :\x01VERSION\x01")

        replies = [(u, m, rest) for u, m, *rest in self.oserve.queued if "VERSION" in m]
        self.assertEqual(len(replies), 1, self.oserve.queued)
        self.assertEqual(replies[0][0], "asker")
        self.assertTrue(replies[0][1].startswith("NOTICE asker :\x01VERSION "), replies[0][1])
        self.assertEqual(replies[0][2], [True], "queued as VIP")

    def test_with_the_reply_off_nothing_is_queued(self):
        self.set_config(CTCP_VERSION_REPLY=False)
        self.after_001(":asker!~u@asker.example PRIVMSG SomeBot :\x01VERSION\x01")

        self.assertEqual([m for _u, m, *_ in self.oserve.queued if "VERSION" in m], [])


for _name in [n for n in dir(own.DrivesPastRegistration) if n.startswith("test")]:
    setattr(TheGatesInTheLoop, _name, None)


if __name__ == "__main__":
    unittest.main()
