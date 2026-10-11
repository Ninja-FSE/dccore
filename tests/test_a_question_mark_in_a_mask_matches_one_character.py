"""A "?" in a ban mask or an admin host mask matches exactly one character.

In an IRC mask "*" is any run of characters and "?" is any one. The hard-ban
matcher turned only "*" into a wildcard and kept "?" literal, so the usual
forms - "!ban *!*@10.0.0.?", "!ban badnick?" - were accepted, confirmed as
"Added permanent wildcard", listed among the active bans, and matched nobody:
the confirmed-but-not-enforced shape of #225.

Both matchers (hard bans in security.py, ADMIN_HOSTMASKS in adminchat.py) now
build the pattern in one place, security.mask_regex(). Since "?" is a
wildcard, the guards that refuse a mask matching everyone strip it with "*".
"""

import io
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import security  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402


class BanCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        root = self.make_temp_dir(prefix="dccore-qmark-")
        self.set_config(HARD_BANS_FILE=os.path.join(root, "hard_bans.txt"))
        security._ban_notified.clear()
        self.addCleanup(security._ban_notified.clear)

    def ban(self, *patterns):
        with io.open(config.HARD_BANS_FILE, "w", encoding="utf-8") as handle:
            handle.write("\n".join(patterns) + "\n")
        security.forget_hard_ban_rules()

    def blocked(self, nick, host):
        security._ban_notified.clear()
        return not security.check_user_status(nick, host)


class AHardBanWithAQuestionMark(BanCase):

    def test_a_host_mask_matches_one_character_there(self):
        self.ban("*!*@10.0.0.?")

        self.assertTrue(self.blocked("alfa", "~a@10.0.0.7"))
        self.assertFalse(self.blocked("alfa", "~a@10.0.0.17"))
        self.assertFalse(self.blocked("alfa", "~a@10.0.0."))

    def test_a_nick_mask(self):
        self.ban("badnick?")

        self.assertTrue(self.blocked("badnick1", "~b@host.example"))
        self.assertFalse(self.blocked("badnick", "~b@host.example"))
        self.assertFalse(self.blocked("badnick12", "~b@host.example"))

    def test_a_host_pattern(self):
        self.ban("*.dialup.example.?om")

        self.assertTrue(self.blocked("bravo", "~b@x.dialup.example.com"))
        self.assertFalse(self.blocked("bravo", "~b@x.dialup.example.org"))

    def test_the_star_and_everything_else_mean_what_they_did(self):
        self.ban("*!*@10.0.0.*", "alfa[x]")

        self.assertTrue(self.blocked("zulu", "~z@10.0.0.200"))
        self.assertTrue(self.blocked("alfa[x]", "~a@host.example"))
        self.assertFalse(self.blocked("alfax", "~a@host.example"))
        self.assertFalse(self.blocked("zulu", "~z@10.0.1.200"))


class AMaskThatMatchesEveryoneIsStillRefused(BanCase):

    EVERYONE = ("?", "???", "?*!*@*", "*!?@*", "*!*@?.?", "?*")

    def test_the_guard_treats_a_question_mark_as_a_wildcard(self):
        for pattern in self.EVERYONE:
            with self.subTest(pattern=pattern):
                self.assertTrue(security.is_over_broad_hard_ban_pattern(pattern))
        self.assertFalse(security.is_over_broad_hard_ban_pattern("*!*@10.0.0.?"))

    def test_enforcement_skips_it(self):
        for pattern in self.EVERYONE:
            with self.subTest(pattern=pattern):
                self.ban(pattern)
                self.assertFalse(self.blocked("abc", "~a@x.y"))

    def test_ban_refuses_it_rather_than_confirming_it(self):
        said = silence_debug(announce)
        commands.handle_hard_ban_request("alfa", "#music", "!ban ?*!*@*", authorised=True)

        self.assertFalse(os.path.exists(config.HARD_BANS_FILE))
        self.assertTrue(any("Refused" in str(line) for line in said), said)


class AnAdminHostMaskWithAQuestionMark(DCCoreTestCase):

    def admin(self, mask, host):
        self.set_config(ADMIN_HOSTMASKS=[mask])
        return adminchat.is_admin_host(":alfa!a@%s PRIVMSG SomeBot :hi" % host)

    def test_it_matches_one_character(self):
        self.assertTrue(self.admin("*!*@operator?.users.example.net", "operator1.users.example.net"))
        self.assertFalse(self.admin("*!*@operator?.users.example.net", "operator12.users.example.net"))
        self.assertFalse(self.admin("*!*@operator?.users.example.net", "operator.users.example.net"))

    def test_one_matcher_for_both(self):
        """A mask means the same thing wherever the bot reads one."""
        for pattern, text, expected in (("a?c", "abc", True), ("a?c", "ac", False),
                                        ("a*c", "abbbc", True), ("a.c", "abc", False),
                                        ("a[b]c", "a[b]c", True)):
            with self.subTest(pattern=pattern, text=text):
                self.assertEqual(bool(security.mask_regex(pattern).match(text)), expected)
                self.assertEqual(bool(adminchat._compile(pattern).match(text)), expected)

    def test_one_that_matches_every_host_is_refused(self):
        for mask in ("?", "?.?", "*?*", "*.?"):
            with self.subTest(mask=mask):
                self.assertFalse(self.admin(mask, "anything.example.net"))

    def test_it_is_reported_as_broad_like_a_star(self):
        self.set_config(ADMIN_HOSTMASKS=["?.users.undernet.org", "*.?rg"])

        broad = dict(adminchat.broad_host_patterns())

        self.assertIn("?.users.undernet.org", broad)
        self.assertIn("*.?rg", broad)


if __name__ == "__main__":
    import unittest
    unittest.main()
