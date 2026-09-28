"""#744: every DCCore was dccore@host, whatever it was called.

irc.py sent `USER <ident> 0 * :<realname>` with getattr(config, 'IDENT',
'dccore') and getattr(config, 'REALNAME', 'dccore bot'). Neither was defined
anywhere, so the fallbacks were always used: a bot named SomeBot appeared on IRC
as dccore@host with the real name "dccore bot". Both now follow the configured
nickname (the ident in lower case, letters and digits only: Undernet refuses more), read at each connection - change NICKNAME and they change with it, with
no new setting.
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


class TheIdent(unittest.TestCase):

    def test_an_ordinary_nick_is_its_own_ident_in_lower_case(self):
        self.assertEqual(irc.ident_for_nick("jlnbln"), "jlnbln")
        self.assertEqual(irc.ident_for_nick("SomeBot"), "somebot")
        self.assertEqual(irc.ident_for_nick("A"), "a")

    def test_the_default_nick_keeps_the_ident_it_always_had(self):
        """DCCore -> dccore: nothing changes for the shipped name."""
        self.assertEqual(irc.ident_for_nick("DCCore"), "dccore")

    def test_it_is_always_lower_case_because_undernet_refuses_anything_else(self):
        for nick in ("UPPER", "MiXeD", "DCCore", "ALLCAPS9"):
            ident = irc.ident_for_nick(nick)
            self.assertEqual(ident, ident.lower(), nick)

    def test_only_ascii_letters_and_digits_are_kept(self):
        self.assertEqual(irc.ident_for_nick("Music[Bot]"), "musicbot")
        self.assertEqual(irc.ident_for_nick("Bot{x}|y"), "botxy")
        self.assertEqual(irc.ident_for_nick("x`y^z"), "xyz")
        self.assertEqual(irc.ident_for_nick("Music-Bot_1"), "musicbot1")
        self.assertEqual(irc.ident_for_nick("a\\b"), "ab")

    def test_it_is_cut_to_the_length_undernet_allows(self):
        self.assertEqual(irc.IDENT_MAX_LENGTH, 10)
        self.assertEqual(irc.ident_for_nick("a-very-long-nickname-indeed"), "averylongn")
        self.assertEqual(len(irc.ident_for_nick("x" * 40)), 10)

    def test_nothing_usable_falls_back_to_dccore(self):
        for nick in ("", None, "[[[[", "_", "---", "{}|"):
            self.assertEqual(irc.ident_for_nick(nick), "dccore", repr(nick))

    def test_non_ascii_never_reaches_the_wire(self):
        self.assertEqual(irc.ident_for_nick("M\u00fasic"), "msic")
        self.assertEqual(irc.ident_for_nick("\u0392\u03bf\u03c4"), "dccore")
        self.assertTrue(irc.ident_for_nick("\u0392\u03bf\u03c4a\u00e9").isascii())

    def test_it_matches_what_the_capture_script_already_found_works(self):
        """scripts/capture_adverts.py derives its own ident the same way."""
        nick = "SomeBot[1]"
        self.assertEqual(irc.ident_for_nick(nick), "".join(c for c in nick.lower() if c.isalnum()))


class TheRegistration(DCCoreTestCase):

    def test_both_follow_the_configured_nickname(self):
        self.set_config(NICKNAME="jlnbln", ORIGINAL_NICK="jlnbln")
        self.assertEqual(irc.registration_names(), ("jlnbln", "DCCore/sc jlnbln"))

    def test_changing_the_nickname_changes_both(self):
        self.set_config(NICKNAME="First", ORIGINAL_NICK="First")
        self.assertEqual(irc.registration_names(), ("first", "DCCore/sc First"))
        self.set_config(NICKNAME="Second", ORIGINAL_NICK="Second")
        self.assertEqual(irc.registration_names(), ("second", "DCCore/sc Second"))

    def test_the_configured_nick_not_the_temporary_alternate(self):
        """First nick taken: NICKNAME is the alternate for a while, ORIGINAL_NICK
        is what was set. The identity does not flicker with that."""
        self.set_config(NICKNAME="DCCore_", ORIGINAL_NICK="jlnbln")
        self.assertEqual(irc.registration_names(), ("jlnbln", "DCCore/sc jlnbln"))

    def test_without_an_original_the_nickname_is_used(self):
        self.set_config(NICKNAME="jlnbln", ORIGINAL_NICK=None)
        self.assertEqual(irc.registration_names(), ("jlnbln", "DCCore/sc jlnbln"))

    def test_nothing_configured_falls_back_as_before(self):
        self.set_config(NICKNAME=None, ORIGINAL_NICK=None)
        self.assertEqual(irc.registration_names(), ("dccore", "DCCore/sc dccore"))

    def test_the_real_name_is_the_mark_and_the_whole_nickname_unchanged(self):
        self.set_config(NICKNAME="Music[Bot]", ORIGINAL_NICK="Music[Bot]")
        self.assertEqual(irc.registration_names(), ("musicbot", "DCCore/sc Music[Bot]"))

    def test_the_old_side_door_is_no_longer_read(self):
        """An IDENT or REALNAME attribute nobody documented is ignored now: one way."""
        self.set_config(NICKNAME="jlnbln", ORIGINAL_NICK="jlnbln", IDENT="other", REALNAME="another")
        self.assertEqual(irc.registration_names(), ("jlnbln", "DCCore/sc jlnbln"))


class TheUserLine(unittest.TestCase):

    def source(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_line_uses_the_derived_names(self):
        source = self.source()
        start = source.index("ident_str, real_str = registration_names()")
        self.assertIn('USER {ident_str} 0 * :{real_str}', source[start:start + 200])

    def test_the_hardcoded_fallbacks_are_gone_from_the_send(self):
        source = self.source()
        self.assertNotIn("getattr(config, 'IDENT'", source)
        self.assertNotIn("getattr(config, 'REALNAME'", source)
        self.assertNotIn("'dccore bot'", source)

    def test_it_is_read_at_each_connection_not_cached_at_import(self):
        """It sits in the handshake, which runs per connection."""
        source = self.source()
        handshake = source[source.index("ident_str, real_str = registration_names()") - 2500:
                           source.index("ident_str, real_str = registration_names()")]
        self.assertIn("def irc_loop", source[:source.index("ident_str, real_str = registration_names()")])
        # Inside the connect loop, after this connection's connect() - not
        # once at the top of irc_loop() (#634 moved USER next to NICK).
        self.assertIn("s.connect(", handshake)


if __name__ == "__main__":
    unittest.main()
