"""FUTURE.md described the flood escalation as a "day-ban"; the code bans
for one hour (audit L33, #697).

FLOOD_BAN_SECONDS ships as 3600, and its own comment explains that the old
midnight expiry was replaced precisely because it could be nearly a day.
The roadmap still said "day-ban", and two comments in security.py did too.
An operator expected a flooder gone for the day and saw them back in an
hour. The roadmap names the setting and its default now; the guard reads
the shipped prose and the code for the word.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults  # noqa: E402

SHIPPED = ["docs/FUTURE.md", "docs/INSTALL.md", "docs/WINDOWS.md", "docs/MACOS.md", "docs/ADMIN-CONSOLE.md",
           "README.md", "security.py", "defaults.py", "settings_help.py", "settings.conf.sample"]


def read(relative):
    path = os.path.join(REPO_ROOT, relative)
    if not os.path.exists(path):
        return ""
    with io.open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


class TheBanIsTimed(unittest.TestCase):

    def test_the_default_is_an_hour_not_a_day(self):
        self.assertEqual(defaults.FLOOD_BAN_SECONDS, 3600)

    def test_nothing_shipped_calls_it_a_day_ban(self):
        hits = [relative for relative in SHIPPED if re.search(r"\bday[- ]bans?\b", read(relative), re.I)]

        self.assertEqual(hits, [])

    def test_the_roadmap_names_the_setting_and_its_default(self):
        roadmap = read("docs/FUTURE.md")

        self.assertIn("escalation to a timed ban (`FLOOD_BAN_SECONDS`, one hour by default)", roadmap)


if __name__ == "__main__":
    unittest.main()
