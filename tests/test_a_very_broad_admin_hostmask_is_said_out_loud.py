"""ADMIN_HOSTMASKS accepted "*.users.undernet.org" and "*.org" without a
word (audit L5, #669).

is_admin_host() refuses only a pattern that reduces to nothing once
wildcards and separators are stripped; a wildcard domain is deliberately
accepted ("*.example.org" names a real set of hosts) and pinned by tests.
But the documented shape is "<account>.users.undernet.org", and an operator
who writes "*.users.undernet.org" instead has put the wildcard where their
account name goes: every X-authenticated user of the network reaches the
password prompt, can hold the single pending session against the real
operator, and costs the bot a dial per CTCP. "*.org" names a top-level
domain. The audit's verdict was that at most a warning is warranted; this
is that warning - at boot, on !rehash and in setup_check.py - and nothing
about what matches has changed.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402

from tests import test_startup as boot  # noqa: E402

ADMIN_LINE = ":op!~op@operator.users.undernet.org PRIVMSG DCCore :hi"
STRANGER_LINE = ":x!~x@anyone.users.undernet.org PRIVMSG DCCore :hi"


def source(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class TheClassifier(unittest.TestCase):

    def setUp(self):
        from tests.support import reset_config
        reset_config()
        self.addCleanup(reset_config)

    def broad(self, *masks):
        config.ADMIN_HOSTMASKS = list(masks)
        return adminchat.broad_host_patterns()

    def test_a_wildcard_where_the_account_goes_is_broad(self):
        found = self.broad("*.users.undernet.org")
        self.assertEqual([p for p, _why in found], ["*.users.undernet.org"])
        self.assertIn("every logged-in user", found[0][1])
        self.assertIn("the account name goes where the * is", found[0][1])

    def test_in_the_familiar_irc_form_too_and_whatever_the_case(self):
        self.assertEqual([p for p, _w in self.broad("*!*@*.users.undernet.org")], ["*.users.undernet.org"])
        self.assertEqual([p for p, _w in self.broad("*.Users.QuakeNet.org")], ["*.users.quakenet.org"])

    def test_a_top_level_domain_is_broad(self):
        found = self.broad("*.org")
        self.assertEqual([p for p, _why in found], ["*.org"])
        self.assertIn("top-level domain .org", found[0][1])

    def test_a_full_host_and_a_legitimate_wildcard_domain_are_not(self):
        self.assertEqual(self.broad("operator.users.undernet.org"), [])
        self.assertEqual(self.broad("*.example.org"), [])
        self.assertEqual(self.broad("op*.users.undernet.org"), [])

    def test_what_is_admin_host_refuses_outright_is_not_listed_twice(self):
        self.assertEqual(self.broad("*.*"), [])
        self.assertEqual(self.broad(""), [])

    def test_the_broad_pattern_still_matches_as_configured(self):
        """The warning is the whole change: what the audit called deliberate
        and test-pinned stays so."""
        config.ADMIN_HOSTMASKS = ["*.users.undernet.org"]

        self.assertTrue(adminchat.is_admin_host(ADMIN_LINE))
        self.assertTrue(adminchat.is_admin_host(STRANGER_LINE))

    def test_the_report_prints_one_warning_per_broad_pattern_and_says_what_to_write(self):
        config.ADMIN_HOSTMASKS = ["*.users.undernet.org", "operator.users.undernet.org", "*.org"]
        said = []

        count = adminchat.report_broad_host_patterns(log=said.append)

        self.assertEqual(count, 2)
        self.assertEqual(len(said), 2)
        self.assertTrue(all(line.startswith("[ADMINCHAT] WARNING: ADMIN_HOSTMASKS entry") for line in said), said)
        self.assertIn("'*.users.undernet.org'", said[0])
        self.assertIn("write it in full, e.g. 'operator.users.undernet.org'", said[0])
        self.assertIn("'*.org'", said[1])

    def test_nothing_is_said_for_a_full_host(self):
        config.ADMIN_HOSTMASKS = ["operator.users.undernet.org"]
        said = []

        self.assertEqual(adminchat.report_broad_host_patterns(log=said.append), 0)
        self.assertEqual(said, [])


class AtBoot(boot.BootCase):

    def test_the_warning_is_in_the_startup_log(self):
        self.set_config(ADMIN_HOSTMASKS=["*.users.undernet.org"])

        out = self.boot(setup_page=False)

        self.assertIn("[ADMINCHAT] WARNING: ADMIN_HOSTMASKS entry '*.users.undernet.org' is very broad", out)

    def test_and_absent_for_a_full_host(self):
        self.set_config(ADMIN_HOSTMASKS=["operator.users.undernet.org"])

        out = self.boot(setup_page=False)

        self.assertNotIn("ADMIN_HOSTMASKS entry", out)


for _name in [n for n in dir(boot.BootCase) if n.startswith("test")]:
    setattr(AtBoot, _name, None)


class OnRehashAndInThePreflight(unittest.TestCase):

    def test_the_rehash_says_it_after_the_reload(self):
        body = source("commands.py")
        start = body.index("def handle_rehash_request")
        handler = body[start:]
        reload = handler.index("reload_modules_in_order()")
        said = handler.index("report_broad_host_patterns()")

        self.assertLess(reload, said, "checked against the modules from before the reload")

    def test_setup_check_warns_rather_than_only_counting(self):
        body = source("scripts/setup_check.py")
        block = body[body.index('ok(f"enabled for {len(patterns)} host pattern(s)")'):][:900]

        self.assertIn("broad_host_patterns()", block)
        self.assertIn("warn(f\"ADMIN_HOSTMASKS entry {pattern!r} is very broad", block)

    def test_the_guide_says_the_broad_shape_is_warned_about(self):
        guide = source("docs/ADMIN-CONSOLE.md")

        self.assertIn("`*.users.undernet.org` puts\nthe wildcard where your account name goes", guide)
        self.assertIn("warned about at start-up, on `!rehash` and\nby `setup_check.py`", guide)


if __name__ == "__main__":
    unittest.main()
