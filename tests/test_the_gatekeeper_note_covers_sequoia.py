"""The macOS Gatekeeper note promised a way in that macOS 15 removed (audit
M60, #662).

The launcher's header, the autostart installer and INSTALL.md all said: the
first time, right-click the .command, choose Open, confirm once. Apple
removed that Control-click override in macOS 15 (Sequoia): after the
refusal the file has to be allowed from System Settings > Privacy &
Security ("Open Anyway"), or de-quarantined with `xattr -d
com.apple.quarantine`. A first-timer on Sequoia following the note got the
same refusal again with no Open button. The header also pointed at a
README-FIRST.txt that has never existed in the repository.

All three texts now give both roads - the old one for macOS 14 and earlier,
Privacy & Security for 15 and later - and the xattr one-liner; the phantom
file is gone. Read here, since the audit found it by reading.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


TEXTS = {
    "the launcher's header": ("scripts", "macos", "start-dccore.command"),
    "the autostart installer": ("scripts", "macos", "install-autostart.command"),
    "INSTALL.md": ("docs", "INSTALL.md"),
}


class EveryNoteGivesBothRoads(unittest.TestCase):

    def test_sequoia_is_sent_to_privacy_and_security(self):
        for name, parts in TEXTS.items():
            with self.subTest(text=name):
                text = read(*parts)
                self.assertIn("Privacy & Security", text)
                self.assertIn("Open Anyway", text)
                self.assertIn("macOS 15", text)

    def test_the_old_road_is_kept_for_the_older_systems(self):
        for name, parts in TEXTS.items():
            with self.subTest(text=name):
                text = read(*parts)
                self.assertIn("macOS 14 and earlier", text)
                # Across a wrapped comment line too ("right-click >
# Open").
                self.assertRegex(text, r"[Rr]ight-click[\s\S]{0,40}Open")

    def test_the_terminal_one_liner_is_given_where_there_is_room(self):
        for name in ("the launcher's header", "INSTALL.md"):
            with self.subTest(text=name):
                self.assertIn("xattr -d com.apple.quarantine scripts/macos/*.command", read(*TEXTS[name]))

    def test_no_note_still_promises_the_removed_override_alone(self):
        """The defect's exact wording: Open, confirm once, done - with no
        alternative for the systems where that is not so."""
        for name, parts in TEXTS.items():
            with self.subTest(text=name):
                self.assertNotIn("choose Open, and confirm once. After that", read(*parts))


class ThePhantomReadme(unittest.TestCase):

    def test_is_not_referred_to(self):
        self.assertNotIn("README-FIRST", read(*TEXTS["the launcher's header"]))
        self.assertFalse(os.path.exists(os.path.join(REPO_ROOT, "README-FIRST.txt")))


if __name__ == "__main__":
    unittest.main()
