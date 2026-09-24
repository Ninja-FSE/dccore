"""ADMIN-CONSOLE.md said configure.py "does steps 2 and 3 together" (audit
M36, #638) - later corrected to "does not do step 3" once configure.py
really did not ask for ADMIN_HOSTMASKS at all.

#811 changed the premise again: configure.py now offers step 3 too, right
after the admin nick question - optionally, blank to skip - and writes
ADMIN_HOSTMASKS to settings.conf when answered. The guide has to say what
actually happens on each branch: answered, it is done; left blank, the
operator is exactly where the "does not do step 3" wording described, and
the troubleshooting entry for a console that answers nobody still applies.

The first class below is the same kind of guard as the M36 fix's own: it
reads configure.py's real prompts, so a further change to what is asked
fails this file first rather than leaving the guide to drift again.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class TheSetupReallyDoesOfferStepThreeNowOptionally(unittest.TestCase):
    """The premise. If configure.py's services-host question is ever
    renamed or removed, the guide's wording below is wrong and this says
    so first."""

    def test_a_prompt_asks_for_the_services_host_right_after_admin_nick(self):
        code = read("configure.py")
        admin_nick_at = code.index('_ask("Admin nick')
        password_at = code.index('_read_password("Password: ")')
        between = code[admin_nick_at:password_at]

        self.assertIn("Your services host", between)
        self.assertIn('changes["ADMIN_HOSTMASKS"]', between)

    def test_a_blank_answer_writes_nothing(self):
        """#891: a blank answer used to fall back to the first existing
        entry and rewrite ADMIN_HOSTMASKS from it - collapsing a multi-host
        list to just that one. The prompt's raw answer must reach
        `if admin_host:` unchanged, with no `or default_host`/similar
        fallback between the input() call and the write it guards."""
        code = read("configure.py")
        # The prompt text is a variable since #911; the first read is the ask.
        prompt_at = code.index("admin_host = input(prompt).strip()")
        line_end = code.index("\n", prompt_at)
        prompt_line = code[prompt_at:line_end]

        self.assertNotIn(" or ", prompt_line,
                         "a blank answer falls back to something instead of staying blank")
        write_at = code.index('changes["ADMIN_HOSTMASKS"] = [f"*!*@{admin_host}"]')
        guard = code[line_end:write_at]
        self.assertIn("if admin_host:", guard)


class TheGuideSaysSo(unittest.TestCase):

    def step_two(self):
        guide = read("docs", "ADMIN-CONSOLE.md")
        return guide.split("### 2. Generate a password hash", 1)[1].split("### 3.", 1)[0]

    def test_configure_is_not_credited_with_doing_step_three_unconditionally(self):
        passage = self.step_two()

        self.assertNotIn("steps 2 and 3", passage)
        self.assertNotIn("does not do step 3", passage,
                         "stale - #811 made step 3 optional here, not absent")

    def test_it_says_the_question_is_optional_and_names_811(self):
        passage = self.step_two()

        self.assertIn("optionally", passage)
        self.assertIn("blank to skip", passage)
        self.assertIn("#811", passage)

    def test_it_says_what_a_blank_answer_leaves_undone(self):
        passage = self.step_two()

        self.assertIn("`ADMIN_HOSTMASKS`", passage)
        self.assertIn("ignores every DCC CHAT without a word", passage)

    def test_and_names_both_places_to_finish_it_by_hand(self):
        passage = self.step_two()

        self.assertIn("by hand as\nstep 3 shows", passage)
        self.assertIn("Settings → Admin console", passage)

    def test_the_troubleshooting_entry_names_the_missing_step(self):
        guide = read("docs", "ADMIN-CONSOLE.md")
        entry = guide.split("**The bot ignores you completely", 1)[1].split("\n**", 1)[0]

        self.assertIn("was never set at all", entry)
        self.assertIn("configure.py", entry)
        self.assertIn("optional", entry)
        self.assertIn("step 3", entry)


if __name__ == "__main__":
    unittest.main()
