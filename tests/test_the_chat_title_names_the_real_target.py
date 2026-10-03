"""The DCCore Chat title says where a typed line really goes (#1041).

mIRC's /var stores a condition as its text - "( == ) || ( == *)" - which is
never empty, and $iif takes any non-empty value as true. So the title always
took the automatic branch and always said "privately to", whatever channel or
peer the operator had picked: the one indicator of where a public line goes
was wrong every time. mIRC cannot run here, so this reads the script's code
lines, comments stripped.
"""

import re
import unittest

from tests import support  # noqa: F401  (path setup)

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_dccore_chat_in_the_mirc_window as chatwin  # noqa: E402


class NoConditionIsStoredAsText(unittest.TestCase):
    def test_no_var_anywhere_holds_a_bare_condition(self):
        """The whole class of the bug, not just this alias."""
        code = chatwin.statements(chatwin.script())
        bare = [line for line in code
                if re.match(r"var %[\w.]+ = \(.*(==|!=|isin|&&|\|\|)", line)]
        self.assertEqual(bare, [])


class TheTitle(unittest.TestCase):
    def setUp(self):
        self.title = chatwin.statements(chatwin.block(chatwin.script(), "alias dccore.chat.title"))

    def test_the_conditions_are_evaluated(self):
        self.assertIn("var %auto = $iif((%to == $null) || (%to == *),1,0)", self.title)
        self.assertIn("var %priv = $iif((%target != $null) && ($left(%target,1) !isin $+($chr(35),&+!)),1,0)",
                      self.title)

    def test_a_pick_is_what_it_names_and_no_pick_is_the_automatic_target(self):
        self.assertIn("var %target = $iif(%auto,$dccore.st(chat.replyto),%to)", self.title)
        self.assertIn("var %where = $iif(%target != $null,%target,every channel with other DCCore bots)",
                      self.title)

    def test_it_says_privately_only_for_a_peer(self):
        line = [s for s in self.title if s.startswith("titlebar $dccore.chat.win")][0]
        self.assertIn("$iif(%priv,privately to,to) %where", line)


if __name__ == "__main__":
    unittest.main()
