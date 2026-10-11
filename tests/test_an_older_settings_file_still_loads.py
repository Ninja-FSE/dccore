"""#1272 review: a settings.conf that worked before must not stop the bot.

The #1272 rules refuse a value that cannot work - a channel without its #, a
list name with a "|", an advert interval under a minute. For a WRITE that is
right: the operator is at the Settings page or configure.py and retypes it.
For a file READ at startup it was not: `CHANNEL = #example-room, jazzroom`
used to join #example-room (only "jazzroom" failed at the server), and
refusing the whole value left CHANNEL blank - a REQUIRED setting - so a bot
that ran yesterday refused to start today. A value read from the file now
keeps whatever part of it works, and the log names what was dropped and the
line it was on. Writes stay strict.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_startup import BootCase  # noqa: E402

MIXED = "#example-room, jazzroom"


class LoadCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.keep_every_setting()
        self.path = settings_file.settings_path()

    def load(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        said = []
        report = settings_file.apply_to(dict(vars(config)), path=self.path, log=said.append)
        return report, said


class AMixedChannelListKeepsItsChannels(LoadCase):

    def test_the_channels_are_kept_and_the_rest_dropped(self):
        report, _said = self.load(f"# mine\nNICKNAME = SampleBot\nCHANNEL = {MIXED}\n")
        self.assertEqual(report["applied"]["CHANNEL"], "#example-room")
        self.assertEqual(report["bad"], [])

    def test_the_log_names_what_was_dropped_and_the_line(self):
        _report, said = self.load(f"# mine\nNICKNAME = SampleBot\nCHANNEL = {MIXED}\n")
        lines = [line for line in said if "CHANNEL" in line]
        self.assertEqual(len(lines), 1, said)
        self.assertIn("line 3", lines[0])
        self.assertIn("'jazzroom'", lines[0])
        self.assertIn("#example-room", lines[0])

    def test_no_channel_at_all_is_still_refused(self):
        report, _said = self.load("CHANNEL = jazzroom, rockroom\n")
        self.assertNotIn("CHANNEL", report["applied"])
        self.assertEqual([name for name, _why in report["bad"]], ["CHANNEL"])

    def test_a_clean_list_says_nothing(self):
        report, said = self.load("CHANNEL = #example-room, #other-room\n")
        self.assertEqual(report["applied"]["CHANNEL"], "#example-room, #other-room")
        self.assertEqual(report["repaired"], [])
        self.assertEqual([line for line in said if "CHANNEL" in line], [])

    def test_a_one_channel_setting_with_a_bad_value_is_ignored_as_before(self):
        report, _said = self.load("DEBUG_CHANNEL = debugroom\n")
        self.assertEqual([name for name, _why in report["bad"]], ["DEBUG_CHANNEL"])


class WritingAMixedListIsStillRefused(LoadCase):

    def test_save(self):
        with self.assertRaises(settings_file.SettingsWriteError):
            settings_file.save(vars(config), {"CHANNEL": MIXED}, path=self.path,
                               log=lambda *_: None)
        self.assertFalse(os.path.exists(self.path))

    def test_the_settings_page(self):
        status, payload = webserver.apply_settings_changes({"CHANNEL": MIXED})
        self.assertEqual(status, 400, payload)


class YesterdaysBotStillStarts(BootCase):
    """The whole point: the REQUIRED gate must not trip on a file that set
    CHANNEL in a shape that used to work."""

    def test_a_mixed_channel_list_boots(self):
        path = settings_file.settings_path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("NICKNAME = SampleBot\n"
                         f"CHANNEL = {MIXED}\n"
                         "ADMIN_NICK = SampleAdmin\n")
        self.set_config(NICKNAME="", CHANNEL="", ADMIN_NICK="")
        settings_file.apply_to(vars(config), path=path, log=lambda *_: None)
        self.assertEqual(config.CHANNEL, "#example-room")
        output = self.boot(setup_page=False)            # must not raise SystemExit
        self.assertIn(config.SCRIPT_VERSION, output)


class AListNameKeepsWorkingWhereItWorked(LoadCase):

    def test_kept_where_the_system_takes_it(self):
        value, note = settings_file.load_value("LIST_BASE_NAME", "DJ|Music", "DCCore", str,
                                               windows=False)
        self.assertEqual(value, "DJ|Music")
        self.assertIn("kept", note)

    def test_sanitised_where_it_could_never_be_a_file(self):
        value, note = settings_file.load_value("LIST_BASE_NAME", "DJ|Music", "DCCore", str,
                                               windows=True)
        self.assertEqual(value, "DJ_Music")
        self.assertIn("'DJ_Music'", note)

    def test_a_name_windows_takes_is_kept_there_too(self):
        """"&" is outside the portable charset a NEW name keeps to, and a
        perfectly good Windows file name."""
        value, _note = settings_file.load_value("LIST_BASE_NAME", "Sample&Co", "DCCore", str,
                                                windows=True)
        self.assertEqual(value, "Sample&Co")

    def test_on_this_system(self):
        report, said = self.load("LIST_BASE_NAME = DJ|Music\n")
        expected = "DJ_Music" if os.name == "nt" else "DJ|Music"
        self.assertEqual(report["applied"]["LIST_BASE_NAME"], expected)
        self.assertTrue(any("LIST_BASE_NAME" in line and "line 1" in line for line in said), said)

    def test_the_settings_page_still_refuses_it(self):
        status, payload = webserver.apply_settings_changes({"LIST_BASE_NAME": "DJ|Music"})
        self.assertEqual(status, 400, payload)


class AShortAdvertIntervalIsRaisedNotReset(LoadCase):

    def test_raised_to_the_minimum_with_a_note(self):
        report, said = self.load("ANNOUNCE_INTERVAL = 10\n")
        self.assertEqual(report["applied"]["ANNOUNCE_INTERVAL"], 60)
        self.assertTrue(any("ANNOUNCE_INTERVAL" in line and "minimum" in line for line in said),
                        said)

    def test_a_value_that_is_not_a_number_is_still_refused(self):
        report, _said = self.load("ANNOUNCE_INTERVAL = often\n")
        self.assertEqual([name for name, _why in report["bad"]], ["ANNOUNCE_INTERVAL"])

    def test_a_good_value_is_untouched(self):
        report, _said = self.load("ANNOUNCE_INTERVAL = 600\n")
        self.assertEqual(report["applied"]["ANNOUNCE_INTERVAL"], 600)
        self.assertEqual(report["repaired"], [])

    def test_the_settings_page_still_refuses_it(self):
        status, payload = webserver.apply_settings_changes({"ANNOUNCE_INTERVAL": "10"})
        self.assertEqual(status, 400, payload)


if __name__ == "__main__":
    unittest.main()
