"""The greedy PART regex took the LAST " PART " in the line, so a part
reason containing " PART " mis-attributed the channel (audit L29, #693).

`^:([^!]+)!.* PART (\S+)` with re.search: the `.*` is greedy, so the
channel group was whatever followed the last " PART <token>" - which may
sit inside the user-typed reason. bob parting #music with reason "I PART
#rock now" while also in #rock was kept in #music (his queue never frozen
while absent, a later send to a channel he had left) and wrongly removed
from #rock (his queue frozen while he sat there). The JOIN parser shared
the greedy `.*`, and an extended-join line carries free text after the
channel too. Both take the first token after the command now, anchored on
the prefix as parse_kick() is, and both are named so they can be tested.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402


class ThePartParser(unittest.TestCase):

    def test_the_audits_line_names_the_channel_actually_left(self):
        self.assertEqual(irc.parse_part(":bob!u@h PART #music :I PART #rock now"), ("bob", "#music"))

    def test_the_ordinary_shapes(self):
        self.assertEqual(irc.parse_part(":bob!u@h PART #music :bye"), ("bob", "#music"))
        self.assertEqual(irc.parse_part(":bob!u@h PART #music"), ("bob", "#music"))
        self.assertEqual(irc.parse_part(":bob!~u@some.host PART &local"), ("bob", "&local"))

    def test_a_part_typed_into_a_channel_is_not_a_part(self):
        """Anchored on the prefix: a PRIVMSG body is not a command."""
        self.assertIsNone(irc.parse_part(":bob!u@h PRIVMSG #c :should i PART #c ?"))
        self.assertIsNone(irc.parse_part("PART #music"))

    def test_the_verifiers_control_the_two_channel_form_is_left_to_the_target_check(self):
        """"#music,#rock" parses as one token, as before; is_valid_irc_target()
        refuses the comma, as before. Not this fix's concern, and not changed."""
        self.assertEqual(irc.parse_part(":bob!u@h PART #music,#rock :x"), ("bob", "#music,#rock"))
        self.assertFalse(irc.is_valid_irc_target("#music,#rock"))


class TheJoinParser(unittest.TestCase):

    def test_with_and_without_the_colon(self):
        self.assertEqual(irc.parse_join(":bob!u@h JOIN :#music"), ("bob", "#music"))
        self.assertEqual(irc.parse_join(":bob!u@h JOIN #music"), ("bob", "#music"))

    def test_an_extended_join_carries_text_after_the_channel(self):
        self.assertEqual(irc.parse_join(":bob!u@h JOIN #music account :Real Name JOIN #x"), ("bob", "#music"))

    def test_a_join_in_a_message_body_is_not_a_join(self):
        self.assertIsNone(irc.parse_join(":bob!u@h PRIVMSG #c :hello JOIN #c"))


class TheReadLoopUsesThem(unittest.TestCase):

    def test_the_handlers_parse_through_the_named_functions(self):
        import io
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            body = handle.read()

        self.assertIn("part_match = parse_part(line)", body)
        self.assertIn("join_match = parse_join(line)", body)
        self.assertNotIn('re.search(r"^:([^!]+)!.* PART', body)
        self.assertNotIn('re.search(r"^:([^!]+)!.* JOIN', body)


if __name__ == "__main__":
    unittest.main()
