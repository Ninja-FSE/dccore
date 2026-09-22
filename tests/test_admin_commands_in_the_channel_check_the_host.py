"""#579: the channel/PM admin commands were gated on the nick alone.

On Undernet a nick is not owned: whoever holds ADMIN_NICK while the operator is
offline had every admin command - `!ban *!*@<the operator's host>` locks the
operator out of their own bot, `!clearqueue` wipes queues, `!update` pauses
serving for a rebuild. irc.py already parses ident@host and the console already
matches ADMIN_HOSTMASKS; the channel commands did neither.

Now, with ADMIN_HOSTMASKS set, the nick AND the host must match. With it empty
the check is the nick alone, as it always was.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import db  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

GOOD = "someident@operator.users.undernet.org"
EVIL = "x@evil.example.net"


class TheCheck(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_NICK="Op,Op2", ADMIN_HOSTMASKS=["*!*@operator.users.undernet.org"])

    def test_the_right_nick_from_the_right_host_is_admin(self):
        self.assertTrue(commands.is_admin("Op", GOOD))
        self.assertTrue(commands.is_admin("op2", GOOD), "case-insensitive, second nick")

    def test_the_right_nick_from_the_wrong_host_is_not(self):
        self.assertFalse(commands.is_admin("Op", EVIL))

    def test_the_right_host_under_the_wrong_nick_is_not(self):
        self.assertFalse(commands.is_admin("Mallory", GOOD))

    def test_a_line_that_does_not_say_where_it_came_from_is_refused(self):
        self.assertFalse(commands.is_admin("Op"))
        self.assertFalse(commands.is_admin("Op", None))
        self.assertFalse(commands.is_admin("Op", ""))

    def test_a_bare_host_works_as_well_as_ident_at_host(self):
        self.assertTrue(commands.is_admin("Op", "operator.users.undernet.org"))
        self.assertTrue(commands.is_admin("Op", ":Op!" + GOOD))

    def test_the_ident_is_not_part_of_the_proof(self):
        """Anybody can set their ident to 'operator'; only the host counts."""
        self.assertFalse(commands.is_admin("Op", "operator@evil.example.net"))

    def test_an_ip_mask_works(self):
        self.set_config(ADMIN_HOSTMASKS=["203.0.113.*"])
        self.assertTrue(commands.is_admin("Op", "u@203.0.113.7"))
        self.assertFalse(commands.is_admin("Op", "u@198.51.100.7"))

    def test_a_pattern_that_would_admit_everyone_admits_no_one(self):
        self.set_config(ADMIN_HOSTMASKS=["*!*@*"])
        self.assertFalse(commands.is_admin("Op", EVIL))

    def test_with_no_hostmasks_the_nick_alone_still_decides(self):
        for empty in ([], None, "", [" "]):
            self.set_config(ADMIN_HOSTMASKS=empty)
            self.assertTrue(commands.is_admin("Op"), empty)
            self.assertTrue(commands.is_admin("Op", EVIL), empty)
            self.assertFalse(commands.is_admin("Mallory", EVIL), empty)

    def test_no_admin_nick_admits_nobody_whatever_the_host(self):
        self.set_config(ADMIN_NICK="")
        self.assertFalse(commands.is_admin("Op", GOOD))

    def test_the_diagnostics_use_the_same_gate(self):
        self.assertTrue(commands.diagnostics_are_for_the_admin("Op", GOOD))
        self.assertFalse(commands.diagnostics_are_for_the_admin("Op", EVIL))
        self.assertFalse(commands.diagnostics_are_for_the_admin("Op"))


class TheCommands(DCCoreTestCase):
    """The five handlers, as irc.py calls them: a nick thief runs nothing."""

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_NICK="Op", ADMIN_HOSTMASKS=["operator.users.undernet.org"])
        self.banned = []
        patch = mock.patch.object(db, "add_hard_ban", lambda pattern, *a, **k: self.banned.append(pattern) or True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_nick_thief_cannot_ban(self):
        commands.handle_hard_ban_request("Op", "#c", "!ban *!*@operator.users.undernet.org", user_host=EVIL)
        self.assertEqual(self.banned, [])

    def test_a_line_without_a_host_cannot_ban(self):
        commands.handle_hard_ban_request("Op", "#c", "!ban *!*@victim.example")
        self.assertEqual(self.banned, [])

    def test_the_operator_can_ban(self):
        commands.handle_hard_ban_request("Op", "#c", "!ban *!*@victim.example", user_host=GOOD)
        self.assertEqual(self.banned, ["*!*@victim.example"])

    def test_the_console_is_unaffected(self):
        """It has proved its own host and a password; it passes authorised=True."""
        commands.handle_hard_ban_request("Whoever", "#c", "!ban *!*@victim.example", authorised=True)
        self.assertEqual(self.banned, ["*!*@victim.example"])

    def test_a_nick_thief_cannot_unban_clear_queues_rehash_or_update(self):
        with mock.patch.object(commands, "_handle_rehash_request") as rehash, \
                mock.patch.object(db, "remove_hard_ban", create=True) as unban, \
                mock.patch.object(db, "save_dcc_queue") as save_queue:
            config.dcc_queue["victim"] = [{"file": "a.flac"}]
            commands.handle_hard_unban_request("Op", "#c", "!unban *!*@x.example", user_host=EVIL)
            commands.handle_admin_clear_queue("Op", "#c", "!clearqueue victim", user_host=EVIL)
            commands.handle_rehash_request("Op", "#c", user_host=EVIL)
            commands.handle_list_update_request("Op", "#c", user_host=EVIL)
        rehash.assert_not_called()
        unban.assert_not_called()
        save_queue.assert_not_called()
        self.assertIn("victim", config.dcc_queue, "the queue was cleared by a nick thief")

    def test_the_operator_can_rehash(self):
        with mock.patch.object(commands, "_handle_rehash_request") as rehash:
            commands.handle_rehash_request("Op", "#c", user_host=GOOD)
        rehash.assert_called_once()


class TheReadLoopPassesTheHost(unittest.TestCase):

    def source(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_every_admin_command_is_handed_the_senders_host(self):
        source = self.source()
        for name in ("handle_rehash_request", "handle_hard_ban_request", "handle_hard_unban_request",
                     "handle_list_update_request", "handle_admin_clear_queue"):
            start = source.index(f"target=commands.{name},")
            self.assertIn('kwargs={"user_host": user_host}', source[start:start + 160], name)

    def test_the_diagnostics_are_handed_it_too(self):
        source = self.source()
        self.assertEqual(source.count("commands.diagnostics_are_for_the_admin(user, user_host)"), 2)
        self.assertNotIn("commands.diagnostics_are_for_the_admin(user):", source)


if __name__ == "__main__":
    unittest.main()
