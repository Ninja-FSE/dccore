"""INSTALL.md said configure.py asks "six questions"; it asks seven, two of
them with follow-ups, then makes two offers (audit L36, #700).

collect_answers() asks nickname, server, channels, admin nick, password,
music folder and dashboard yes/no - the dashboard with a LAN follow-up and
a Flask-install offer - and main() then offers to build the list and to
import OmenServe totals. INSTALL.md listed six and omitted the dashboard;
WINDOWS.md listed a different set; neither mentioned the two offers, so a
novice expecting six was surprised by "Import them now?" with nothing
explaining it. INSTALL.md carries the list now, in the order asked, and
WINDOWS.md points at it. The guard reads the prompts out of configure.py
in source order and requires the guide's numbered items to follow them.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(relative):
    with io.open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


# The prompt in configure.py (a distinctive piece of it, in source order)
# and the words the guide's item for it must carry.
ASKED = [
    ('_ask("Nickname"', "Nickname"),
    ('_ask("IRC server"', "IRC server"),
    ('_ask("Channel(s), comma-separated"', "Channel(s)"),
    ('_ask("Admin nick', "Admin nick"),
    ('input(f"Your services host (blank to skip){suffix}: ")', "services host"),
    ('_read_password("Password: ")', "password"),
    ('input(f"Music directory (full path)', "Music directory"),
    ('input("Enable it? [y/N]: ")', "Web dashboard"),
]
FOLLOW_UPS = [
    ('input("  Create it now? [y/N]: ")', "offers to create it"),
    ('input("  Reachable from other devices on your LAN', "LAN"),
    ('input("  Install it now (pip install -r requirements-web.txt)?', "install it now"),
    ('input("Generate it now? [Y/n]: ")', "generate the file list now"),
    ('ask("Import them now? [y/N]: ")', "import your OmenServe totals"),
]


class TheGuideFollowsTheCode(unittest.TestCase):

    def setUp(self):
        self.code = read("configure.py")
        guide = read("docs/INSTALL.md")
        start = guide.index("What it asks, in order:")
        self.section = guide[start:guide.index("The music directory is optional here", start)]
        self.items = re.findall(r"^\d+\. \*\*(.+?)\*\*", self.section, re.M)

    def test_every_prompt_is_in_configure_py_where_this_test_thinks_it_is(self):
        """The guard reads real prompts; a renamed one must be renamed here."""
        for prompt, _words in ASKED + FOLLOW_UPS:
            self.assertIn(prompt, self.code, prompt)

    def test_the_questions_are_listed_one_each_in_the_order_asked(self):
        positions = [self.code.index(prompt) for prompt, _w in ASKED]
        self.assertEqual(positions, sorted(positions), "ASKED is not in source order")
        self.assertEqual(len(self.items), len(ASKED), self.items)
        for item, (_prompt, words) in zip(self.items, ASKED):
            self.assertIn(words.lower(), item.lower(), (item, words))

    def test_the_follow_ups_and_the_two_offers_are_named(self):
        for _prompt, words in FOLLOW_UPS:
            self.assertIn(words, self.section, words)

    def test_nothing_says_six_questions_any_more(self):
        for relative in ("docs/INSTALL.md", "docs/WINDOWS.md", "README.md"):
            self.assertNotRegex(read(relative), r"[Ss]ix questions")

    def test_windows_md_points_at_the_list(self):
        self.assertIn("[INSTALL.md](INSTALL.md#what-configure-asks)", read("docs/WINDOWS.md"))
        self.assertIn('<a id="what-configure-asks"></a>', read("docs/INSTALL.md"))


if __name__ == "__main__":
    unittest.main()
