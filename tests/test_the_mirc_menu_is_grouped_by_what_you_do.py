"""The @DCCore right-click menu is grouped by what the operator is doing (#1112).

It had grown without a plan: Stop the bot sat under Library, Reload under Admin,
Console command stood alone and nine queries and toggles filled the top level.
Now: Script Settings and the Console command first, then Info, Lists, Library,
User control and Control, then Connection and Window; the two windows are in
Window and the command list is in Info. Stop the bot and Reload are last in Control, behind
a separator, and still ask first.

mIRC is not available here; the menu is a plain list in dccore.mrc, so this
reads it and builds the tree from the indentation and the leading dots.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SEPARATOR = "-"


def menu_lines():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    start = text.index("menu @DCCore {")
    return text[start:text.index("\n}\n", start)].split("\n")[1:]


def parse():
    """(top, groups): top is the top level in order as (label, action) with
    action None for a group; groups maps a group's name to its items, as
    (label, action), a separator being ("-", None)."""
    top, groups, current = [], {}, None
    for line in menu_lines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("."):
            text = text[1:]
            label, _, action = text.partition(":")
            groups[current].append((label, action or None) if text != SEPARATOR else (SEPARATOR, None))
        elif text == SEPARATOR:
            top.append((SEPARATOR, None))
            current = None
        elif ":" in text:
            label, _, action = text.partition(":")
            top.append((label, action))
            current = None
        else:
            top.append((text, None))
            groups[text] = []
            current = text
    return top, groups


def names(items):
    return [label for label, _ in items]


class TheTopLevel(unittest.TestCase):

    def test_the_groups_come_in_this_order(self):
        top, _ = parse()
        groups = [label for label, action in top if action is None and label != SEPARATOR]
        self.assertEqual(groups, ["Info", "Lists", "Library", "User control", "Control", "Connection", "Window"])

    def test_the_rest_of_the_top_level_is_the_two_tools_the_nick_items_and_clear_finished(self):
        top, _ = parse()
        loose = [label for label, action in top if action is not None]
        self.assertEqual([label for label in loose if not label.startswith("$iif($dccore.sel")],
                         ["Script Settings", "Console command", "Clear finished..."])

    def test_the_settings_and_the_console_command_come_first(self):
        top, _ = parse()
        self.assertEqual(names(top)[:3], ["Script Settings", "Console command", SEPARATOR])
        self.assertEqual(dict(top)["Script Settings"], "dccore.options")

    def test_the_two_tools_are_named_without_dots_and_the_window_is_last_but_one(self):
        top, _ = parse()
        self.assertNotIn("Options...", names(top))
        self.assertEqual(names(top)[-2:], ["Window", "Clear finished..."])

    def test_the_old_top_level_questions_and_the_old_admin_group_are_gone(self):
        top, groups = parse()
        for label in ("Status", "Slots", "Queue", "Bans", "Uptime", "Version", "Admin", "Command list",
                      "DCCore Chat", "Downloads window", "Clear finished downloads"):
            self.assertNotIn(label, names(top), label)
        self.assertNotIn("Admin", groups)


class TheGroups(unittest.TestCase):

    def setUp(self):
        _, self.groups = parse()

    def test_info_holds_the_questions_and_ends_with_the_command_list(self):
        self.assertEqual(names(self.groups["Info"]),
                         ["Status", "Slots", "Queue", "Uptime", "Version", SEPARATOR, "Command list"])
        self.assertEqual(dict(self.groups["Info"])["Command list"], "dccore")

    def test_window_holds_the_two_windows_then_the_look_of_this_one(self):
        self.assertEqual(names(self.groups["Window"]),
                         ["DCCore Chat", "Downloads window", SEPARATOR, "Panel $iif($dccore.opt(panel),off,on)",
                          "Font size...", "Dashboard address...", "Clear window"])
        self.assertNotIn("Options...", names(self.groups["Window"]))

    def test_user_control_holds_the_bans_and_queues_of_nicks(self):
        self.assertEqual(names(self.groups["User control"]), ["Bans", "Ban...", "Unban...", "Clear a queue..."])

    def test_library_is_only_about_the_library(self):
        self.assertEqual(names(self.groups["Library"]), ["Find duplicate filenames", "Rebuild the list..."])

    def test_control_ends_with_reload_and_then_stop_behind_a_separator(self):
        items = self.groups["Control"]
        self.assertEqual([label for label, _ in items][-3:], [SEPARATOR, "Reload the bot (rehash)...", "Stop the bot..."])
        self.assertEqual(names(items)[:3][0], "Check for a new version")

    def test_the_toggles_keep_their_live_labels(self):
        labels = names(self.groups["Control"])
        self.assertTrue(any(label.startswith("Daily update check $iif($dccore.st(checkupdates)") for label in labels))
        self.assertTrue(any(label.startswith("Console feed $iif($dccore.st(consolefeed)") for label in labels))

    def test_stop_and_reload_are_nowhere_else(self):
        for name, items in self.groups.items():
            if name == "Control":
                continue
            for label in names(items):
                self.assertNotIn("Stop the bot", label, name)
                self.assertNotIn("rehash", label, name)

    def test_stop_and_reload_still_ask_first(self):
        actions = dict(self.groups["Control"])
        self.assertTrue(actions["Reload the bot (rehash)..."].startswith("dccore.confirm rehash "))
        stop = actions["Stop the bot..."]
        self.assertIn("$input(", stop)
        self.assertIn("dccore.send shutdown now", stop)
        self.assertNotIn("dccore.send shutdown now", "".join(a for l, a in self.groups["Control"] if a and l != "Stop the bot..."))


class ClearFinishedAsksFirst(unittest.TestCase):

    def test_the_main_menu_and_the_downloads_window_ask_before_forgetting_the_list(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
            text = handle.read()
        asks = "dccore.confirm dlclear Clear the list of finished downloads? Only the list is cleared and no files are deleted."
        self.assertIn("\n  Clear finished...:" + asks + "\r\n", text)
        self.assertIn("\n  Clear the finished ones...:" + asks + "\r\n", text)
        self.assertNotIn("dccore.send dlclear", text)


# Every action the menu had before the regrouping (#1112); each is still in it.
OLD_ACTIONS = [
    "dccore.send status", "dccore.send slots", "dccore.send queue", "dccore.send bans",
    "dccore.send uptime", "dccore.send version", "dccore.send checkversion",
    "dccore.send checkupdates $iif($dccore.st(checkupdates) == on,off,on)",
    "dccore.send consolefeed $iif($dccore.st(consolefeed) == on,off,on)",
    "dccore.send queue $dccore.selq", "dccore.send clearqueue $dccore.selq", "dccore.send queue $dccore.sels",
    "dccore lists", "dccore fetch", "dccore.ask fetch Ask which bot for its list",
    "dccore.send verify",
    "dccore.confirm update Rebuild the list? It walks the whole library and can take minutes.",
    "dccore.ask ban Ban pattern (for example *!*@host.example)", "dccore.ask unban Pattern to remove",
    "dccore.ask clearqueue Clear the queue of which nick",
    "dccore.confirm rehash Reload the bot's code and settings?", "dccore.askraw",
    "dccore $iif($chat($dccore.bot),disconnect,connect)", "dccore pair $dccore.bot", "dccore unpair",
    "dccore trust", "dccore.options", "dccore panel $iif($dccore.opt(panel),off,on)", "dccore.askfont",
    "dccore weburl", "clear @DCCore", "dccore chat", "dccore downloads", "dccore.confirm dlclear Clear the list of finished downloads? Only the list is cleared and no files are deleted.", "dccore",
]


class NothingWasLost(unittest.TestCase):

    def test_every_action_of_the_old_menu_is_still_in_it(self):
        top, groups = parse()
        actions = {action for _, action in top if action}
        for items in groups.values():
            actions.update(action for _, action in items if action)
        for old in OLD_ACTIONS:
            self.assertIn(old, actions, old)

    def test_each_action_is_once_in_the_menu(self):
        top, groups = parse()
        every = [action for _, action in top if action]
        for items in groups.values():
            every.extend(action for _, action in items if action)
        for action in OLD_ACTIONS:
            if action in ("dccore.send queue",):
                continue
            self.assertEqual(every.count(action), 1, action)

    def test_the_selected_nick_items_sit_between_control_and_connection(self):
        top, _ = parse()
        labels = names(top)
        first = next(i for i, label in enumerate(labels) if label.startswith("$iif($dccore.selq"))
        self.assertGreater(first, labels.index("Control"))
        self.assertLess(first, labels.index("Connection"))


class TheScriptVersion(unittest.TestCase):

    def test_a_new_menu_is_a_feature_so_the_minor_moves(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
            match = re.search(r"alias dccore\.ver \{ return ([0-9.]+) \}", handle.read())
        self.assertGreaterEqual(tuple(int(p) for p in match.group(1).split(".")), (1, 11))


if __name__ == "__main__":
    unittest.main()
