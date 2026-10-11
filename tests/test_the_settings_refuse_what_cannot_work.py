"""#1272: values the Settings page, settings.conf and configure.py accepted
although the bot could not work with them.

  * ANNOUNCE_INTERVAL = 0 - the advert worker stopped sleeping and flooded.
  * FILE_DIRECTORY pointing nowhere - saved fine, then the next restart
    refused to boot, taking the dashboard it was set from with it.
  * CHANNEL "music" or "#music #rock" - JOIN went to no channel, or joined
    #music with "#rock" as its key. The browser setup refused both.
  * LIST_BASE_NAME "DJ|Music" - every list rebuild failed on Windows.

All four are checked in settings_file, which every path into config goes
through, so each is tested at coerce(), at save() and at the dashboard's
own endpoint.
"""

import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class SettingsCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.keep_every_setting()
        self.home = self.make_temp_dir(prefix="dccore-refuse-")
        self.path = os.path.join(self.home, "settings.conf")

    def coerce(self, name, raw):
        types = settings_file.declared_types(vars(config))
        return settings_file.coerce(name, raw, getattr(config, name), types.get(name))

    def save(self, **changes):
        return settings_file.save(vars(config), changes, path=self.path, log=lambda *_: None)

    def refused_by_save(self, **changes):
        with self.assertRaises(settings_file.SettingsWriteError) as caught:
            self.save(**changes)
        self.assertFalse(os.path.exists(self.path), "a refused save wrote the file")
        return str(caught.exception)

    def applied_from_file(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        namespace = dict(vars(config))
        return settings_file.apply_to(namespace, path=self.path, log=lambda *_: None)


class TheAdvertIntervalHasAMinimum(SettingsCase):

    def test_zero_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.coerce("ANNOUNCE_INTERVAL", "0")
        self.assertIn("minimum of 60", str(caught.exception))

    def test_a_negative_value_and_one_just_under_are_refused(self):
        for raw in ("-5", "59"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.coerce("ANNOUNCE_INTERVAL", raw)

    def test_the_minimum_itself_and_the_default_are_fine(self):
        self.assertEqual(self.coerce("ANNOUNCE_INTERVAL", "60"), 60)
        self.assertEqual(self.coerce("ANNOUNCE_INTERVAL", "300"), 300)

    def test_the_settings_page_refuses_it_before_anything_is_written(self):
        status, payload = webserver.apply_settings_changes({"ANNOUNCE_INTERVAL": "0"})
        self.assertEqual(status, 400, payload)
        self.assertIn("ANNOUNCE_INTERVAL", payload["error"])
        self.assertFalse(os.path.exists(settings_file.settings_path()))

    def test_a_hand_edited_zero_is_raised_to_the_minimum(self):
        """Read, not written: the file is older than the rule, so the value is
        clamped and said, not thrown away (tests/test_an_older_settings_file_
        still_loads.py has the rest of that rule)."""
        report = self.applied_from_file("ANNOUNCE_INTERVAL = 0\n")
        self.assertEqual(report["applied"]["ANNOUNCE_INTERVAL"], 60)
        self.assertEqual(report["bad"], [])

    def test_other_numbers_are_untouched(self):
        """0 means "off" for plenty of neighbours; only the named one has a floor."""
        self.assertEqual(self.coerce("REJOIN_ATTEMPTS", "0"), 0)


class AMusicFolderMustExist(SettingsCase):

    def test_a_folder_that_is_not_there_is_refused(self):
        typo = os.path.join(self.home, "Muisc")
        message = self.refused_by_save(FILE_DIRECTORY=typo)
        self.assertIn("does not exist", message)

    def test_an_existing_folder_is_saved(self):
        music = os.path.join(self.home, "Music")
        os.makedirs(music)
        self.assertEqual(self.save(FILE_DIRECTORY=music)["written"], ["FILE_DIRECTORY"])

    def test_a_quoted_path_says_the_quotes_are_the_problem(self):
        music = os.path.join(self.home, "My Music")
        os.makedirs(music)
        message = self.refused_by_save(FILE_DIRECTORY=f'"{music}"')
        self.assertIn("quotes", message)

    def test_blank_is_still_allowed(self):
        """Not chosen yet is not misconfigured - the daemon boots without one."""
        self.save(FILE_DIRECTORY="")
        self.assertTrue(os.path.exists(self.path))

    def test_the_folder_already_set_is_kept_while_its_drive_is_away(self):
        """Only a change is checked: re-saving the configured folder while its
        drive is unplugged - the console's unchanged round trip - is not refused."""
        away = os.path.join(self.home, "UnpluggedDrive", "Music")
        self.set_config(FILE_DIRECTORY=away)
        self.assertEqual(settings_file.check_change(vars(config), "FILE_DIRECTORY", away), away)
        self.assertIn("does not exist", self.refused_by_save(FILE_DIRECTORY=away + "2"))

    def test_the_settings_page_answers_400(self):
        typo = os.path.join(self.home, "Muisc")
        status, payload = webserver.apply_settings_changes({"FILE_DIRECTORY": typo})
        self.assertEqual(status, 400, payload)
        self.assertIn("FILE_DIRECTORY", payload["error"])


class AChannelIsAChannel(SettingsCase):

    def test_the_shared_rule(self):
        self.assertIsNone(settings_file.channels_problem("#example-room"))
        self.assertIsNone(settings_file.channels_problem("#example-room, #other-room"))
        self.assertIsNone(settings_file.channels_problem("&local-room"))
        for bad in ("example-room", "#example-room #other-room", "#", "#a\x07b", ",,"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(settings_file.channels_problem(bad))

    def test_coerce_refuses_a_channel_without_its_hash(self):
        with self.assertRaises(ValueError):
            self.coerce("CHANNEL", "example-room")

    def test_coerce_refuses_channels_separated_by_spaces(self):
        with self.assertRaises(ValueError) as caught:
            self.coerce("CHANNEL", "#example-room #other-room")
        self.assertIn("commas", str(caught.exception))

    def test_a_list_still_saves(self):
        self.save(CHANNEL="#example-room,#other-room")
        with open(self.path, encoding="utf-8") as handle:
            written = settings_file.parse(handle.read())
        self.assertEqual(written["CHANNEL"], "#example-room,#other-room")

    def test_the_settings_page_refuses_what_the_setup_page_refuses(self):
        status, payload = webserver.apply_settings_changes({"CHANNEL": "example-room"})
        self.assertEqual(status, 400, payload)

    def test_the_one_channel_settings_take_one_channel(self):
        for name in ("DEBUG_CHANNEL", "BROADCAST_SEARCH_CHANNEL"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.coerce(name, "#example-room,#other-room")
                with self.assertRaises(ValueError):
                    self.coerce(name, "example-room")
                self.assertEqual(self.coerce(name, "#example-room"), "#example-room")

    def test_blank_debug_channel_is_still_none(self):
        self.assertIn(self.coerce("DEBUG_CHANNEL", ""), ("", None))

    def test_the_browser_setup_and_the_shared_rule_agree(self):
        """One rule: the page's verdict is the shared validator's, sample by sample."""
        from tests.test_set_it_up_in_the_browser import GOOD
        for value in ("#example-room", "example-room", "#example-room #other-room",
                      "&local-room", "#a, #b"):
            with self.subTest(value=value):
                form = dict(GOOD, CHANNEL=value)
                _changes, _hash, errors = webserver.validate_setup_form(form)
                page_refused = any(field == "CHANNEL" for field, _msg in errors)
                self.assertEqual(page_refused,
                                 settings_file.channels_problem(value) is not None)


class AListNameIsAFileName(SettingsCase):

    def test_a_pipe_is_refused_with_the_name_it_would_derive(self):
        message = self.refused_by_save(LIST_BASE_NAME="DJ|Music")
        self.assertIn("DJ_Music", message)
        self.assertIn("'|'", message)

    def test_a_backslash_and_a_trailing_dot_are_refused(self):
        for bad in ("DJ\\Music", "SampleBot."):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.coerce("LIST_BASE_NAME", bad)

    def test_a_legal_name_saves(self):
        for good in ("DJ_Music", "Sample[Bot]", "sample-bot.v2"):
            with self.subTest(good=good):
                self.assertEqual(self.coerce("LIST_BASE_NAME", good), good)

    def test_a_hand_edited_bad_name_is_kept_or_sanitised_never_dropped(self):
        """Dropping it would fall back to the nickname and rename the published
        list; see tests/test_an_older_settings_file_still_loads.py."""
        report = self.applied_from_file("LIST_BASE_NAME = DJ|Music\n")
        self.assertIn(report["applied"]["LIST_BASE_NAME"], ("DJ|Music", "DJ_Music"))

    def test_the_derived_name_follows_the_same_rule(self):
        """One charset for the derived name and the typed one."""
        for nick in ("DJ|Music", "Sample\\Bot", "Plain"):
            with self.subTest(nick=nick):
                derived = config._sanitize_list_base_name(nick)
                self.assertEqual(derived, settings_file.sanitize_list_base_name(nick))
                self.assertIsNone(settings_file.list_base_name_problem(derived))


if __name__ == "__main__":
    unittest.main()
