"""Every /dccore command, and every console command worth a click, is in the
right-click menu of the @DCCore window.

There were many to remember. The menu is a plain list in dccore.mrc, so this
reads it: each command has an entry, the ones that change something or take
minutes ask first, the ones that need a word ask for it, and Cancel sends
nothing. mIRC is not available here; the prompts are $input calls (mIRC 6.0+).
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


def script():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def block(header):
    text = script()
    start = text.index(header)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError(header)


def alias_body(name):
    return block("alias " + name + " {")


class TheWindowMenu(unittest.TestCase):

    def setUp(self):
        self.menu = block("menu @DCCore {")

    def test_the_console_commands_that_are_worth_a_click_are_all_there(self):
        for command in ("status", "slots", "queue", "bans", "uptime", "version", "verify", "lists", "fetch",
                        "ban", "unban", "clearqueue", "rehash", "update"):
            self.assertRegex(self.menu, r"(dccore\.send|dccore\.ask|dccore\.confirm|dccore) " + command + r"\b", command)

    def test_the_slash_commands_are_all_there(self):
        for command in ("pair", "unpair", "trust", "connect", "disconnect", "options", "panel", "font", "lists", "fetch"):
            self.assertIn(command, self.menu, command)
        self.assertIn("dccore.options", self.menu)
        self.assertIn("clear @DCCore", self.menu)

    def test_every_dccore_command_the_script_implements_has_an_entry(self):
        text = script()
        implemented = set(re.findall(r"if \(%cmd == (\w+)\)", text))
        # `window` opens the window this menu is already in, `raw` is the
        # free-text "Console command...", `status` is the top item.
        needed = implemented - {"window", "raw", "version"}
        for command in sorted(needed):
            self.assertRegex(self.menu, r"\bdccore(\.send)? " + command + r"\b|\." + command + r"\b|dccore\.options|\bdccore \$iif",
                             command)

    def test_every_console_command_that_is_not_plumbing_has_an_entry(self):
        plumbing = {"hello", "help", "quit", "pair", "unpair"}
        for command in adminchat.COMMANDS:
            if command in plumbing:
                continue
            self.assertRegex(self.menu, r"\b" + command + r"\b", f"{command} is in the console but not the menu")

    def test_the_command_list_is_reachable(self):
        self.assertIn("Command list:dccore\n", self.menu)

    def test_the_quick_views_are_still_at_the_top_and_the_nick_items_remain(self):
        self.assertLess(self.menu.index("Status:dccore.send status"), self.menu.index("Lists"))
        self.assertIn("Queue of $dccore.selq", self.menu)
        self.assertIn("Queue of $dccore.sels", self.menu)

    def test_the_submenus_use_the_dot_form(self):
        for entry in (".Show the lists:", ".Fetch the changed lists:", ".Ask a bot for its list...:", ".Find duplicate filenames:",
                      ".Ban...:", ".Unban...:", ".Clear a queue...:", ".Options...:", ".Font size...:"):
            self.assertIn(entry, self.menu, entry)

    def test_the_things_that_change_or_take_minutes_ask_first(self):
        self.assertIn(".Rebuild the list...:dccore.confirm update ", self.menu)
        self.assertIn(".Reload the bot (rehash)...:dccore.confirm rehash ", self.menu)
        self.assertNotIn("dccore.send update", self.menu)
        self.assertNotIn("dccore.send rehash", self.menu)

    def test_the_ones_that_need_a_word_ask_for_it(self):
        self.assertIn("dccore.ask ban ", self.menu)
        self.assertIn("dccore.ask unban ", self.menu)
        self.assertIn("dccore.ask clearqueue ", self.menu)
        self.assertIn(".Ask a bot for its list...:dccore.ask fetch ", self.menu)
        self.assertNotRegex(self.menu, r"dccore\.send fetch\b", "a named fetch needs the bot's nick")
        self.assertIn("Console command...:dccore.askraw", self.menu)
        self.assertNotRegex(self.menu, r"dccore\.send ban\b", "a ban needs a pattern")
        self.assertNotRegex(self.menu, r"dccore\.send unban\b")

    def test_the_menu_is_not_open_to_a_typo_in_the_prompt_text(self):
        """A comma would end the argument of $input."""
        for line in self.menu.split("\n"):
            if "dccore.ask " in line or "dccore.confirm " in line:
                prompt = line.split("dccore.ask ", 1)[-1] if "dccore.ask " in line else line.split("dccore.confirm ", 1)[-1]
                self.assertNotIn(",", prompt, line)


class ThePrompts(unittest.TestCase):

    def test_ask_sends_the_command_and_the_answer(self):
        body = alias_body("dccore.ask")
        self.assertIn("var %v = $input($2-,eo,DCCore)", body)
        self.assertIn("dccore.send $1 %v", body)

    def test_cancel_or_an_empty_answer_sends_nothing(self):
        for name in ("dccore.ask", "dccore.askraw"):
            self.assertIn("if (%v != $null)", alias_body(name), name)

    def test_the_raw_prompt_sends_the_line_as_typed(self):
        body = alias_body("dccore.askraw")
        self.assertIn("$input(", body)
        self.assertIn("dccore.send %v", body)

    def test_confirm_sends_only_on_yes(self):
        body = alias_body("dccore.confirm")
        self.assertIn("if ($input($2-,yq,DCCore)) { dccore.send $1 }", body)

    def test_the_font_prompt_only_accepts_a_number(self):
        body = alias_body("dccore.askfont")
        self.assertIn("if (%v isnum) { dccore font %v }", body)

    def test_the_raw_prompt_text_has_no_comma(self):
        body = alias_body("dccore.askraw")
        prompt = re.search(r"\$input\(([^)]*\)[^)]*)\)", body).group(1)
        self.assertNotIn(",eo,DCCore,", prompt)


class TheOtherMenus(unittest.TestCase):

    def test_the_status_and_channel_menu_offers_lists_and_the_command_list(self):
        menu = block("menu status,channel {")
        for entry in (".Show the lists:dccore lists", ".Fetch the changed lists:dccore fetch", ".Command list:dccore"):
            self.assertIn(entry, menu)
        self.assertIn(".Open the window:dccore window", menu)

    def test_the_nicklist_menu_is_unchanged(self):
        menu = block("menu nicklist {")
        self.assertIn(".Queue of $1:dccore.send queue $1", menu)
        self.assertIn(".Clear the queue of $1:dccore.send clearqueue $1", menu)


if __name__ == "__main__":
    unittest.main()
