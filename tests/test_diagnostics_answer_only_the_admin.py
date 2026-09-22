"""!ping and !debugnames answer the bot's own admin, and nobody else.

Seen live by the user: another operator typed !ping in a shared channel to
check their own bot, and every DCCore in the channel ran a latency check -
each spending a paced server line (the slot the adverts share) and each
reporting into its own admin console, so a stranger's ping appeared in the
user's console as if the user had asked. Neither command even answers the
person who typed it: !ping reports only to the operator, and !debugnames
is a notice about the bot's own membership mirror. They are the operator's
tools, so they answer the operator - the same is_admin() the admin
commands use.

!list stays public on purpose: it is the discovery command every serving
bot answers with its trigger, which is how people find bots.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402


class WhoMayRunThem(DCCoreTestCase):
    def test_the_admin_may(self):
        self.set_config(ADMIN_NICK="SysOp")
        self.assertTrue(commands.diagnostics_are_for_the_admin("SysOp"))
        self.assertTrue(commands.diagnostics_are_for_the_admin("sysop"), "case-insensitive, like is_admin")

    def test_anyone_else_may_not(self):
        self.set_config(ADMIN_NICK="SysOp")
        for nick in ("dave", "SysOp2", "", None):
            with self.subTest(nick=nick):
                self.assertFalse(commands.diagnostics_are_for_the_admin(nick))

    def test_every_listed_admin_may(self):
        self.set_config(ADMIN_NICK="SysOp, Second")
        self.assertTrue(commands.diagnostics_are_for_the_admin("second"))

    def test_it_is_the_same_rule_as_the_admin_commands(self):
        """One gate, not a second one that could drift."""
        self.set_config(ADMIN_NICK="SysOp")
        for nick in ("SysOp", "dave"):
            self.assertEqual(commands.diagnostics_are_for_the_admin(nick), commands.is_admin(nick))


class AStrangersPingDoesNothing(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_NICK="SysOp", MSG_DELAY=0.0)
        runtime.outbound_pacer = runtime.OutboundPacer()
        self.addCleanup(setattr, runtime, "outbound_pacer", runtime.OutboundPacer())
        config.ping_start_time = None
        self.sock = RecordingSocket()

    def test_no_ping_leaves_the_bot_for_a_stranger(self):
        commands.handle_ping_request(self.sock, "dave", "#chan")

        self.assertEqual(self.sock.sent, [], "a stranger's !ping made the bot send to the server")
        self.assertIsNone(config.ping_start_time, "and no measurement was started")

    def test_the_admins_ping_still_goes_out(self):
        commands.handle_ping_request(self.sock, "SysOp", "#chan")

        self.assertEqual(len(self.sock.sent), 1)
        self.assertIn(b"PING", self.sock.sent[0])
        self.assertIsNotNone(config.ping_start_time)


class TheDispatchGatesBothBeforeDoingAnything(unittest.TestCase):
    """Read from the source: the two branches live inside irc_loop(). The
    property is that each checks the admin gate first and `continue`s -
    before the thread for !ping is spawned and before !debugnames builds
    its notice - and that !list, next to them, does not."""

    def branch(self, marker):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()
        start = source.index(marker)
        return source[start:start + 900]

    def test_ping_checks_first(self):
        body = self.branch('elif msg.lower() == "!ping":')
        gate = body.index("commands.diagnostics_are_for_the_admin(user, user_host)")
        self.assertLess(gate, body.index("handle_ping_request"))
        self.assertIn("continue", body[gate:body.index("handle_ping_request")])

    def test_debugnames_checks_first(self):
        body = self.branch('elif msg.lower() == "!debugnames":')
        gate = body.index("commands.diagnostics_are_for_the_admin(user, user_host)")
        self.assertLess(gate, body.index("channel_users_lock()"))
        self.assertIn("continue", body[gate:body.index("channel_users_lock()")])

    def test_list_stays_public(self):
        body = self.branch('elif msg_lower == "!list":')
        first = body[:body.index("elif", 5)]
        self.assertNotIn("diagnostics_are_for_the_admin", first)
        self.assertIn("send_list_trigger_info", first)


if __name__ == "__main__":
    unittest.main()
