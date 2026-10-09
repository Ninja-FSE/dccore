"""The mIRC menus reach the ignore and queue-order commands (#1206, part 3).

Right-click a nick in a channel: Ignore for..., Clear the queue and ignore
for..., Stop ignoring. The @DCCore window has the same for the selected queue
row and a prompt for each console command. mIRC is not available here, so this
reads dccore.mrc: the ask for minutes is a $input call whose prompt text has no
commas, a bad answer sends nothing, and clear-and-ignore sends ignore first.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
from tests.test_the_mirc_menu_has_every_command import block  # noqa: E402


def script_text():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
        return handle.read()


class TheNicklistMenu(unittest.TestCase):

    def setUp(self):
        self.menu = block("menu nicklist {")

    def test_ignore_for_a_while_is_there(self):
        self.assertIn(".Ignore $1 for...:dccore.ignorefor $1", self.menu)

    def test_clear_and_ignore_is_there(self):
        self.assertIn(".Clear the queue of $1 and ignore for...:dccore.clearignore $1", self.menu)

    def test_lifting_the_ignore_needs_no_question(self):
        self.assertIn(".Stop ignoring $1:dccore.send unignore $1", self.menu)

    def test_the_old_items_are_still_there(self):
        self.assertIn(".Queue of $1:dccore.send queue $1", self.menu)
        self.assertIn(".Clear the queue of $1:dccore.send clearqueue $1", self.menu)


class TheWindowMenu(unittest.TestCase):

    def setUp(self):
        self.menu = block("menu @DCCore {")

    def test_the_user_control_submenu_asks_for_each_new_command(self):
        for command in ("ignore", "unignore", "queuemove", "queueremove"):
            self.assertRegex(self.menu, r"dccore\.ask " + command + r" ", command)

    def test_the_selected_row_can_be_ignored_or_cleared_and_ignored(self):
        self.assertIn("$iif($dccore.selq,Ignore $dccore.selq for...):dccore.ignorefor $dccore.selq", self.menu)
        self.assertIn("$iif($dccore.selq,Clear the queue of $dccore.selq and ignore for...):"
                      "dccore.clearignore $dccore.selq", self.menu)

    def test_the_selected_row_can_move_up_and_down(self):
        self.assertIn("dccore.send queuemove $dccore.selq up", self.menu)
        self.assertIn("dccore.send queuemove $dccore.selq down", self.menu)

    def test_the_prompts_have_no_commas(self):
        for line in self.menu.splitlines():
            match = re.search(r"dccore\.ask (?:ignore|unignore|queuemove|queueremove) (.*)$", line)
            if match:
                self.assertNotIn(",", match.group(1), line)


class TheMinutesQuestion(unittest.TestCase):

    def setUp(self):
        self.text = script_text().replace("\r\n", "\n")
        self.minutes = block("alias dccore.minutes {")

    def test_it_asks_with_an_input_box_that_starts_at_thirty(self):
        self.assertRegex(self.minutes, r"\$input\([^,]*,eo,DCCore,30\)")

    def test_the_prompt_text_has_no_commas(self):
        prompt = re.search(r"\$input\((.*?),eo,DCCore,30\)", self.minutes).group(1)
        self.assertNotIn(",", prompt)

    def test_only_a_whole_number_from_one_to_the_bot_s_limit_is_returned(self):
        self.assertIn("(%v isnum 1-10080) && (. !isin %v) { return %v }", self.minutes)

    def test_a_bad_answer_is_said_and_cancel_is_silent(self):
        self.assertIn("if (%v != $null) { dccore.sys", self.minutes)
        self.assertIn("whole number of minutes from 1 to 10080", self.minutes)

    def test_the_limit_is_the_one_the_bot_enforces(self):
        import security
        self.assertEqual(security.IGNORE_MAX_MINUTES, 10080)


class TheTwoAliases(unittest.TestCase):

    def test_ignore_for_sends_nothing_without_minutes(self):
        body = block("alias dccore.ignorefor {")
        self.assertIn("if (%m) { dccore.send ignore $1 %m }", body)

    def test_clear_and_ignore_sends_one_combined_command(self):
        """#1247: used to send `ignore` and `clearqueue` as two separate,
        unconditional commands - a nick the ignore refused still had its
        queue cleared regardless, as if the ignore had worked. One command
        lets the bot itself decide whether the clear ever happens."""
        body = block("alias dccore.clearignore {")
        self.assertIn("dccore.send clearandignore $1 %m", body)
        self.assertNotIn("dccore.send ignore", body)
        self.assertNotIn("dccore.send clearqueue", body)

    def test_clear_and_ignore_sends_nothing_without_minutes(self):
        body = block("alias dccore.clearignore {")
        self.assertIn("if (%m) {", body)
        self.assertNotIn("dccore.send clearandignore $1 %m\n  dccore", body)


class TheVersion(unittest.TestCase):

    def test_the_script_is_1_17_1(self):
        self.assertIn("alias dccore.ver { return 1.17.1 }", script_text().replace("\r\n", "\n"))

    def test_it_still_draws_the_queues_window(self):
        self.assertTrue(adminchat.script_draws_dlqueue("1.16"))


if __name__ == "__main__":
    unittest.main()
