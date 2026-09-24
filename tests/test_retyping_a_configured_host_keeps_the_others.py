"""Retyping a services host that is already configured keeps the others (#911).

#909 fixed #891's reported case - Enter at the services-host prompt no longer
collapses several hosts to the first. Two ways the same step still lost or
misread hosts:

- typing a host that is already configured (out of habit, or because the
  prompt no longer offers it as a default) wrote [just that one] - with
  home and phone set, retyping home dropped the phone, the loss #891 was
  about, reached by typing instead of by Enter;
- a comma-separated ADMIN_HOSTMASKS (a form adminchat accepts, and
  admin_config.py may hold) was indexed as a string: its first CHARACTER
  was the "first host", and len() counted characters.

A new host still replaces the list, with #909's warning; only these two
change. Driven through collect_answers() with canned input.
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
import configure  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

HOME = "me.users.undernet.org"
PHONE = "my-phone.example.net"


class ARerun(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        real = adminchat._read_password
        self.addCleanup(setattr, adminchat, "_read_password", real)

    def run_with_host_answers(self, *host_answers):
        answers = iter(["MyBot", "", "#my-channel", "MyAdmin", *host_answers,
                        self.tree.music, "n"])
        passwords = iter(["secret123", "secret123"])
        adminchat._read_password = lambda prompt: next(passwords)
        import builtins
        real_input = builtins.input
        builtins.input = lambda prompt="": next(answers)
        said = io.StringIO()
        try:
            with contextlib.redirect_stdout(said):
                changes, _hash = configure.collect_answers()
        finally:
            builtins.input = real_input
        return changes, said.getvalue()

    def test_retyping_a_configured_host_keeps_the_others(self):
        """The reported case."""
        self.set_config(ADMIN_HOSTMASKS=["*!*@" + HOME, "*!*@" + PHONE])

        changes, said = self.run_with_host_answers(HOME)

        self.assertNotIn("ADMIN_HOSTMASKS", changes,
                         "retyping the home host dropped the phone")
        self.assertIn("Already configured - nothing changed.", said)
        self.assertNotIn("This replaces", said)

    def test_whatever_its_case(self):
        self.set_config(ADMIN_HOSTMASKS=["*!*@" + HOME, "*!*@" + PHONE])

        changes, _said = self.run_with_host_answers(PHONE.upper())

        self.assertNotIn("ADMIN_HOSTMASKS", changes)

    def test_every_configured_host_is_shown(self):
        self.set_config(ADMIN_HOSTMASKS=["*!*@" + HOME, "*!*@" + PHONE])

        _changes, said = self.run_with_host_answers("")

        self.assertIn("Configured now: %s, %s" % (HOME, PHONE), said)

    def test_a_comma_separated_setting_is_read_as_hosts_not_characters(self):
        self.set_config(ADMIN_HOSTMASKS="*!*@%s,*!*@%s" % (HOME, PHONE))

        changes, said = self.run_with_host_answers("new.users.undernet.org")

        self.assertIn("Configured now: %s, %s" % (HOME, PHONE), said)
        self.assertIn("This replaces the 2 services hosts", said,
                      "the count was of characters, not hosts")
        self.assertEqual(changes["ADMIN_HOSTMASKS"], ["*!*@new.users.undernet.org"])

    def test_retyping_one_from_a_comma_separated_setting_changes_nothing(self):
        self.set_config(ADMIN_HOSTMASKS="*!*@%s,*!*@%s" % (HOME, PHONE))

        changes, _said = self.run_with_host_answers(PHONE)

        self.assertNotIn("ADMIN_HOSTMASKS", changes)

    def test_a_new_host_still_replaces_the_list_with_909s_warning(self):
        """Unchanged: only a host already there is treated differently."""
        self.set_config(ADMIN_HOSTMASKS=["*!*@" + HOME, "*!*@" + PHONE])

        changes, said = self.run_with_host_answers("laptop.users.undernet.org")

        self.assertEqual(changes["ADMIN_HOSTMASKS"], ["*!*@laptop.users.undernet.org"])
        self.assertIn("This replaces the 2 services hosts already configured with just this one.", said)


if __name__ == "__main__":
    unittest.main()
