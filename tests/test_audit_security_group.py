"""Two silent failures on the paths that are supposed to be strict.

Both found by the full-program audit.

1. A HOST-SHAPED HARD BAN WAS CONFIRMED, LISTED, AND NEVER ENFORCED.

   check_user_status() decided a pattern was "hostmask-shaped" only if it
   contained "!" or "@". Anything else was matched against the bare NICK - and
   an IRC nick can never contain a dot, so

       !ban *.dialup.example.com

   could not match anything, ever. But !ban accepted it, reported success, and
   db.load_hard_bans() listed it among the active bans. The admin believed a
   host was banned; the host walked straight in.

   The same shape as #225, where the confirmation and the enforcement
   disagreed silently - which is the reason is_over_broad_hard_ban_pattern()
   was lifted out to be shared in the first place.

2. A BOT NICK CONTAINING "|" BROKE EVERY FETCH FROM THAT BOT.

   _sanitize_bot_dir_name() claimed in its own docstring to apply "the same
   discipline" as dcc_fetch._sanitize_offer_filename(), which is a WHITELIST.
   It was a blacklist: NUL, the two separators, ".." and surrounding dots.

   "|" is an ordinary IRC nick character - RFC 2812's specials are []\\`_^{|}
   and "Bot|Away" is one of the commonest nick shapes on the network - and is
   illegal in a Windows path. So os.makedirs() on the extraction directory
   failed with WinError 123 AFTER the zip had already come over DCC. The
   transfer worked, the bytes were on disk, and the fetch failed at the last
   step, every time, for that bot.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import list_fetch  # noqa: E402
import security  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class AHostShapedBanIsMatchedAgainstTheHost(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.hard = os.path.join(self.make_tree().root, "hard_bans.txt")
        self.set_config(HARD_BANS_FILE=self.hard)

    def ban(self, pattern):
        with io.open(self.hard, "w", encoding="utf-8", newline="") as handle:
            handle.write(pattern + "\n")

    def denied(self, nick, mask):
        return security.check_user_status(nick, hostmask=mask) is False

    MASK = "someone!~ident@host12.dialup.example.com"

    def test_a_dotted_pattern_now_matches_the_host(self):
        """The way an admin actually types it."""
        self.ban("*.dialup.example.com")

        self.assertTrue(self.denied("someone", self.MASK))

    def test_the_full_hostmask_form_still_works(self):
        """Control: the spelling that always worked must go on working."""
        self.ban("*!*@*.dialup.example.com")

        self.assertTrue(self.denied("someone", self.MASK))

    def test_an_ip_pattern_matches_an_ip_host(self):
        self.ban("192.168.1.*")

        self.assertTrue(self.denied("someone", "someone!~i@192.168.1.44"))

    def test_an_ipv6_pattern_is_host_shaped_too(self):
        """A colon cannot appear in a nick either."""
        self.ban("2001:db8:*")

        self.assertTrue(self.denied("someone", "someone!~i@2001:db8::1"))

    def test_a_bare_nick_pattern_still_matches_the_nick(self):
        """The other half. Narrowing "what counts as host-shaped" must not
        stop a plain nick ban working."""
        self.ban("baduser")

        self.assertTrue(self.denied("baduser", "baduser!~i@wherever.example.net"))

    def test_a_nick_pattern_does_not_match_a_different_nick(self):
        self.ban("baduser")

        self.assertFalse(self.denied("someone", self.MASK))

    def test_a_host_pattern_does_not_match_an_unrelated_host(self):
        """The fix must not turn every dotted pattern into a wildcard."""
        self.ban("*.dialup.example.com")

        self.assertFalse(self.denied("someone", "someone!~i@cable.other.net"))

    def test_it_still_works_with_no_hostmask_supplied(self):
        """Older callers pass only a nick. A host-shaped pattern then has
        nothing to match against, and must not raise or match wrongly."""
        self.ban("*.dialup.example.com")

        self.assertFalse(security.check_user_status("someone") is False)


class ABotNickIsNotAPathComponent(unittest.TestCase):

    def test_a_pipe_is_replaced(self):
        """The commonest nick shape on the network, and illegal in a Windows
        path."""
        self.assertNotIn("|", list_fetch._sanitize_bot_dir_name("Bot|Away"))

    def test_every_character_windows_refuses(self):
        for nick in ('Bot|Away', 'Bot*star', 'Bot"q', "Bot<a>", "Bot?x",
                     "Bot:col"):
            with self.subTest(nick=nick):
                name = list_fetch._sanitize_bot_dir_name(nick)

                for bad in '|*"<>?:':
                    self.assertNotIn(bad, name)

    def test_the_result_can_actually_be_created(self):
        import tempfile

        root = tempfile.mkdtemp()
        for nick in ('Bot|Away', 'Bot*star', 'Bot"q', "Good_Bot", "Bot[EU]"):
            with self.subTest(nick=nick):
                target = os.path.join(
                    root, list_fetch._sanitize_bot_dir_name(nick))

                os.makedirs(target, exist_ok=True)

                self.assertTrue(os.path.isdir(target))

    def test_legal_nick_specials_are_kept(self):
        """RFC 2812 specials that ARE legal in a path stay, so the directory is
        still recognisably that bot's."""
        name = list_fetch._sanitize_bot_dir_name("Bot[EU]{x}^_`")

        self.assertIn("[EU]", name)
        self.assertIn("{x}", name)

    def test_an_ordinary_nick_is_unchanged(self):
        self.assertEqual(list_fetch._sanitize_bot_dir_name("Good_Bot-2"),
                         "Good_Bot-2")

    def test_separators_and_traversal_are_still_removed(self):
        """The blacklist's original job, which the whitelist must not lose."""
        name = list_fetch._sanitize_bot_dir_name("../../etc/passwd")

        self.assertNotIn("..", name)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)

    def test_a_nick_of_nothing_usable_still_yields_a_name(self):
        for nick in ("", "...", "///", "\x00"):
            with self.subTest(nick=nick):
                self.assertTrue(list_fetch._sanitize_bot_dir_name(nick))

    def test_it_is_a_whitelist_as_the_docstring_claims(self):
        """The docstring said "the same discipline as
        dcc_fetch._sanitize_offer_filename()" while doing the opposite. A
        blacklist cannot enumerate what a future filesystem will refuse."""
        exotic = list_fetch._sanitize_bot_dir_name("Bot\u202e\x00\ufeff x")

        self.assertTrue(all(ch.isalnum() or ch in "_-.[]{}^`" for ch in exotic),
                        f"unexpected characters survived: {exotic!r}")


if __name__ == "__main__":
    unittest.main()
