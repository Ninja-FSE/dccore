"""Writing settings.conf back, for a dashboard that offers a settings page.

The reading half has existed since settings.conf did. This is the writing half,
and it is the more dangerous one: it edits the file that configures a running
daemon, and the operator's own copy of it, on their machine.

WHY EDITING RATHER THAN REWRITING

settings.conf starts life as a copy of settings.conf.sample - every setting
present, commented out, under section headers, with the explanation for each
one above it. Operators uncomment the few they care about and add notes of
their own. A writer that rebuilt the file from a dict of values would throw all
of that away on the first save, so this one replaces lines and leaves every
other byte alone.

WHY ONE MISTAKE WOULD BE INVISIBLE AND TOTAL

parse() refuses a file where a key appears twice, on purpose. But that refusal
is all-or-nothing: it raises for the whole file, apply_to() catches it, and the
daemon starts with every setting back at its default. A writer that appended a
key already present would therefore not break that setting - it would break all
of them, at the next restart, with nothing to connect the two events.

So the class that matters most here is TheFileIsNotTouchedUnlessItIsRight: for
every way a save can go wrong, the assertion is that the bytes on disk did not
change.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


SAMPLE = os.path.join(REPO_ROOT, "settings.conf.sample")


class WriterTestCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = self.make_tree().root
        self.path = os.path.join(self.dir, "settings.conf")

    def write(self, text):
        with io.open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return text

    def read(self):
        with io.open(self.path, "rb") as handle:
            return handle.read().decode("utf-8")

    def save(self, changes, **kwargs):
        kwargs.setdefault("log", lambda message: None)
        return settings_file.save(vars(config), changes, path=self.path, **kwargs)

    def every_setting(self):
        """Every real setting, as config.py declares them.

        From the annotations rather than vars(config): the live module also
        carries names the daemon and the test harness set while running -
        ORIGINAL_NICK, MY_IP_OR_DOCK - which look exactly like settings and are
        not. tests/test_settings_file.py reads config.py's source for the same
        reason.
        """
        return {name: getattr(config, name) for name in config.__annotations__
                if settings_file.is_overridable(name, getattr(config, name, None))}

    def value_of(self, name):
        """What the daemon would read back for `name`."""
        entries = settings_file.parse(self.read())
        types = settings_file.declared_types(vars(config))
        return settings_file.coerce(name, entries[name],
                                    getattr(config, name), types.get(name))


class EditingRatherThanRewriting(WriterTestCase):

    def test_a_commented_default_is_uncommented_where_it_stands(self):
        """The common case, because settings.conf is a copy of the sample: the
        setting is already there, commented out, under its own explanation."""
        self.write("[limits]\n"
                   "# How many people may download at once.\n"
                   "#MAX_DCC_SLOTS = 3\n")

        self.save({"MAX_DCC_SLOTS": 9})

        self.assertEqual(self.read(),
                         "[limits]\n"
                         "# How many people may download at once.\n"
                         "MAX_DCC_SLOTS = 9\n")

    def test_an_existing_value_is_replaced_in_place(self):
        self.write("[limits]\nMAX_DCC_SLOTS = 3\n")

        self.save({"MAX_DCC_SLOTS": 9})

        self.assertEqual(self.read(), "[limits]\nMAX_DCC_SLOTS = 9\n")

    def test_everything_else_is_left_byte_for_byte(self):
        """The operator's file, not ours. Their comments, their sections, their
        blank lines, their ordering."""
        before = self.write(
            "# my own notes, please keep\n"
            "\n"
            "[irc network]\n"
            "#NICKNAME = DCCore\n"
            "\n"
            "\t# indented note\n"
            "[limits]\n"
            "#MAX_DCC_SLOTS = 3\n"
            "# trailing thought\n")

        self.save({"MAX_DCC_SLOTS": 9})
        after = self.read()

        changed = [(a, b) for a, b in zip(before.split("\n"), after.split("\n")) if a != b]
        self.assertEqual(len(changed), 1, "more than the one line changed: %r" % changed)
        self.assertEqual(changed[0], ("#MAX_DCC_SLOTS = 3", "MAX_DCC_SLOTS = 9"))

    def test_a_setting_that_appears_nowhere_is_appended(self):
        self.write("[limits]\n#MAX_DCC_SLOTS = 3\n")

        report = self.save({"ANNOUNCE_INTERVAL": 600})

        self.assertEqual(report["added"], ["ANNOUNCE_INTERVAL"])
        self.assertIn("ANNOUNCE_INTERVAL = 600", self.read())
        self.assertEqual(self.value_of("ANNOUNCE_INTERVAL"), 600)

    def test_an_appended_setting_says_where_it_came_from(self):
        """A line appearing at the end of a hand-kept file with no explanation
        is a small mystery. This leaves a note."""
        self.write("[limits]\n")

        self.save({"ANNOUNCE_INTERVAL": 600})

        self.assertIn("Added by DCCore", self.read())

    def test_an_empty_file_is_a_fine_starting_point(self):
        self.write("")

        self.save({"MAX_DCC_SLOTS": 9})

        self.assertEqual(self.value_of("MAX_DCC_SLOTS"), 9)

    def test_no_file_at_all_is_a_fine_starting_point(self):
        self.assertFalse(os.path.exists(self.path))

        self.save({"MAX_DCC_SLOTS": 9})

        self.assertEqual(self.value_of("MAX_DCC_SLOTS"), 9)

    def test_nothing_to_save_writes_nothing(self):
        self.write("[limits]\n#MAX_DCC_SLOTS = 3\n")

        self.save({})

        self.assertEqual(self.read(), "[limits]\n#MAX_DCC_SLOTS = 3\n")

    def test_an_active_line_wins_over_a_commented_one(self):
        """The sample's commented default stays a commented default, and the
        line the operator actually uncommented is the one that changes. Editing
        the comment instead would leave the old active value in force."""
        self.write("#MAX_DCC_SLOTS = 3\n"
                   "[limits]\n"
                   "MAX_DCC_SLOTS = 5\n")

        self.save({"MAX_DCC_SLOTS": 9})

        self.assertEqual(self.read(), "#MAX_DCC_SLOTS = 3\n[limits]\nMAX_DCC_SLOTS = 9\n")
        self.assertEqual(self.value_of("MAX_DCC_SLOTS"), 9)

    def test_a_prose_comment_naming_a_setting_is_not_rewritten(self):
        """"# MAX_DCC_SLOTS: how many at once" is a sentence, not a commented
        default. Only "#KEY = value" is one - which is the shape the sample
        writes - so a note an operator left for themselves survives."""
        self.write("# MAX_DCC_SLOTS: how many people can download at once\n"
                   "#MAX_DCC_SLOTS = 3\n")

        self.save({"MAX_DCC_SLOTS": 9})

        self.assertEqual(
            self.read(),
            "# MAX_DCC_SLOTS: how many people can download at once\n"
            "MAX_DCC_SLOTS = 9\n")

    def test_a_file_with_windows_line_endings_keeps_them(self):
        """Reading in text mode turns CRLF into LF, so writing back with LF
        would rewrite every line of a file last edited in Notepad - a diff of
        the whole file for a one-setting change."""
        self.write("[limits]\r\n#MAX_DCC_SLOTS = 3\r\n")

        self.save({"MAX_DCC_SLOTS": 9})

        after = self.read()
        self.assertEqual(after, "[limits]\r\nMAX_DCC_SLOTS = 9\r\n")
        self.assertEqual(after.replace("\r\n", "").count("\n"), 0)


class TheFileIsNotTouchedUnlessItIsRight(WriterTestCase):
    """Every refusal, and in each one the same assertion: the bytes on disk did
    not change.

    A caller that catches SettingsWriteError knows the file is exactly as it
    was, which is what makes it safe to hand this to a web form.
    """

    def setUp(self):
        super().setUp()
        self.before = self.write(
            "[limits]\n"
            "# keep me\n"
            "#MAX_DCC_SLOTS = 3\n"
            "#NICKNAME = DCCore\n")

    def refuse(self, changes):
        with self.assertRaises(settings_file.SettingsWriteError) as caught:
            self.save(changes)
        self.assertEqual(self.read(), self.before,
                         "the file was modified despite the save being refused")
        return str(caught.exception)

    def test_a_setting_this_version_does_not_have(self):
        why = self.refuse({"NO_SUCH_SETTING": "1"})

        self.assertIn("NO_SUCH_SETTING", why)

    def test_a_name_the_daemon_sets_while_it_runs(self):
        """MY_IP_OR_DOCK is the address detected at startup, not a preference.
        It is an uppercase string attribute of config like any setting, so a
        settings page built from vars(config) would offer it - and writing it
        would freeze one session's answer in place for every session after it,
        with apply_to() dutifully applying it and the detection never running
        again."""
        config.MY_IP_OR_DOCK = "203.0.113.9"
        self.addCleanup(lambda: vars(config).pop("MY_IP_OR_DOCK", None))

        why = self.refuse({"MY_IP_OR_DOCK": "203.0.113.9"})

        self.assertIn("while it runs", why)

    def test_a_value_that_will_not_convert(self):
        why = self.refuse({"MAX_DCC_SLOTS": "quite a lot"})

        self.assertIn("whole number", why)

    def test_a_value_containing_a_line_break(self):
        """One setting per line. The rest of the value would be read as a new
        setting, or as a syntax error, depending on what it happened to say."""
        why = self.refuse({"NICKNAME": "DCCore\nMAX_DCC_SLOTS = 99"})

        self.assertIn("line break", why)

    def test_a_value_with_surrounding_spaces(self):
        """Stripped when the file is read, so it cannot come back as it went
        in. Refused rather than silently trimmed: the operator typed it."""
        why = self.refuse({"NICKNAME": " DCCore "})

        self.assertIn("spaces", why)

    def test_the_wrong_type_is_named_along_with_what_it_would_become(self):
        """CHANNEL is a comma-separated STRING in config.py, not a list, and a
        caller that guesses wrong should be told what it would have got rather
        than "the file would not be readable".

        This is the check that fires before the file is even built. The
        whole-file check at the end would refuse the same save, so this exists
        for the message: it names the setting, the value given, and the value
        it would have been read back as.
        """
        why = self.refuse({"CHANNEL": ["#one", "#two"]})

        self.assertIn("CHANNEL", why)
        self.assertIn("#one", why)
        self.assertIn("read back", why)

    def test_a_list_entry_containing_a_comma(self):
        """Entries are comma-separated, so one would come back as two."""
        why = self.refuse({"ADMIN_HOSTMASKS": ["*!*@a,b.example"]})

        self.assertIn("comma", why)

    def test_a_file_that_already_sets_a_key_twice(self):
        """Already broken before the save - parse() refuses it too. Editing one
        of the two and leaving the other would look like a fix and not be one,
        so it says which lines instead."""
        self.before = self.write("MAX_DCC_SLOTS = 3\n[limits]\nMAX_DCC_SLOTS = 5\n")

        why = self.refuse({"MAX_DCC_SLOTS": 9})

        self.assertIn("twice", why)
        self.assertIn("1", why)

    def test_a_file_the_operator_has_already_broken_by_hand(self):
        """settings.conf is a text file an operator edits, so it can already be
        unparseable before the dashboard touches it - a stray line, an unclosed
        section header. Editing it would produce something still unparseable,
        and apply_to() would then start the daemon with every setting back at
        its default. Refused, and said out loud, rather than saved."""
        self.before = self.write("#MAX_DCC_SLOTS = 3\n"
                                 "this line is not a setting\n")

        why = self.refuse({"MAX_DCC_SLOTS": 9})

        self.assertIn("not be readable", why)
        self.assertIn("unchanged", why)

    def test_a_line_the_edit_turns_into_part_of_another_value(self):
        """The whole-file check earns its place here, because no amount of
        checking one value in isolation could see this.

        configparser treats an INDENTED line as a continuation of the setting
        above it. So uncommenting a default that happens to be followed by an
        indented line silently glues the two together: the file would have said
        NICKNAME = "DCCoreWin\\ncontinued", which is not what anybody asked for
        and not something a nickname can be.
        """
        self.before = self.write("#NICKNAME = DCCore\n"
                                 "    continued\n")

        why = self.refuse({"NICKNAME": "DCCoreWin"})

        self.assertIn("NICKNAME", why)
        self.assertIn("read back", why)

    def test_a_setting_the_edit_makes_disappear_entirely(self):
        """The other half of the same hazard. An indented setting is not a
        setting at all - it is a continuation of the line above - so the edit
        would land in the file, look right to a human reading the diff, and the
        daemon would never see MAX_DCC_SLOTS at all.
        """
        self.before = self.write("NICKNAME = DCCore\n"
                                 "    #MAX_DCC_SLOTS = 3\n")

        why = self.refuse({"MAX_DCC_SLOTS": 9})

        self.assertIn("MAX_DCC_SLOTS", why)
        self.assertIn("did not survive", why)

    def test_the_operators_own_mistake_is_reported_before_the_files(self):
        """Values are checked before the file is even read, so somebody who
        posts the wrong shape for a setting is told about the value they sent
        rather than sent looking for a problem in a file that also happens to
        be broken.

        CHANNEL is a comma-separated STRING in config.py, not a list.
        """
        self.before = self.write("this file is broken too\n")

        why = self.refuse({"CHANNEL": ["#one", "#two"]})

        self.assertIn("would be read back as", why)
        self.assertNotIn("not be readable", why)

    def test_one_bad_value_stops_the_whole_save(self):
        """All of it or none of it. A settings page that half-saved would leave
        the daemon in a state the operator never asked for and cannot see."""
        self.refuse({"MAX_DCC_SLOTS": 9, "NICKNAME": "DCCore\nbad"})

        self.assertNotIn("MAX_DCC_SLOTS = 9", self.read())

    def test_the_check_runs_before_anything_is_opened_for_writing(self):
        """No temporary file is left behind by a refused save either."""
        self.refuse({"MAX_DCC_SLOTS": "not a number"})

        strays = [n for n in os.listdir(self.dir) if n.startswith(".tmp_")]
        self.assertEqual(strays, [])


class ValuesArriveAsStringsFromAForm(WriterTestCase):
    """A web form sends text for everything, including the settings that are
    not text. A caller holding real Python values should get the same result,
    so both are accepted and both are read the file's way."""

    def setUp(self):
        super().setUp()
        self.write("")

    def test_a_number_typed_into_a_box(self):
        self.save({"MAX_DCC_SLOTS": "9"})

        self.assertEqual(self.value_of("MAX_DCC_SLOTS"), 9)

    def test_a_checkbox(self):
        for text, expected in (("true", True), ("yes", True), ("on", True),
                               ("false", False), ("no", False), ("0", False)):
            with self.subTest(text=text):
                self.save({"DEBUG_MODE": text})
                self.assertIs(self.value_of("DEBUG_MODE"), expected)

    def test_a_comma_separated_list(self):
        self.save({"ADMIN_HOSTMASKS": "*!*@one.example, *!*@two.example"})

        self.assertEqual(self.value_of("ADMIN_HOSTMASKS"),
                         ["*!*@one.example", "*!*@two.example"])

    def test_the_same_setting_typed_or_as_text_gives_the_same_file(self):
        self.save({"MAX_DCC_SLOTS": 9, "DEBUG_MODE": True})
        typed = self.read()

        self.write("")
        self.save({"MAX_DCC_SLOTS": "9", "DEBUG_MODE": "true"})

        self.assertEqual(self.read(), typed)

    def test_a_setting_whose_default_is_none_can_be_cleared(self):
        """RAR_BINARY means "look on PATH" when unset, and an empty value in
        the file means the same thing rather than an empty string."""
        self.save({"RAR_BINARY": ""})

        self.assertIsNone(self.value_of("RAR_BINARY"))


class WhatWasSavedIsWhatTheDaemonReads(WriterTestCase):
    """The point of the whole exercise. config.py ends with
    apply_to(globals()), and !rehash reloads config.py - so a save followed by
    a rehash is what makes a settings page work at all."""

    def test_a_saved_value_comes_back_through_apply_to(self):
        self.write("")
        self.save({"MAX_DCC_SLOTS": 9, "NICKNAME": "DCCoreWin",
                   "DEBUG_MODE": True, "ADMIN_HOSTMASKS": ["*!*@example"]})

        namespace = dict(vars(config))
        report = settings_file.apply_to(namespace, path=self.path,
                                        log=lambda message: None)

        self.assertEqual(report["bad"], [])
        self.assertEqual(report["unknown"], [])
        self.assertEqual(namespace["MAX_DCC_SLOTS"], 9)
        self.assertEqual(namespace["NICKNAME"], "DCCoreWin")
        self.assertIs(namespace["DEBUG_MODE"], True)
        self.assertEqual(namespace["ADMIN_HOSTMASKS"], ["*!*@example"])

    def test_saving_twice_updates_rather_than_duplicates(self):
        """The failure that would break every setting rather than one. Saving
        the same key again must edit the line it wrote the first time."""
        self.write("")
        self.save({"MAX_DCC_SLOTS": 9})
        self.save({"MAX_DCC_SLOTS": 4})

        self.assertEqual(self.read().count("MAX_DCC_SLOTS ="), 1)
        self.assertEqual(self.value_of("MAX_DCC_SLOTS"), 4)

    def test_saving_every_overridable_setting_leaves_a_readable_file(self):
        """The heaviest thing a settings page can do: post the whole form back
        with every field in it. All 51 have to survive together."""
        self.write("")
        every = self.every_setting()
        self.assertGreater(len(every), 40, "the settings list is unexpectedly short")

        self.save(every)

        namespace = dict(vars(config))
        report = settings_file.apply_to(namespace, path=self.path,
                                        log=lambda message: None)
        self.assertEqual(report["bad"], [])
        self.assertEqual(report["unknown"], [])
        for name, value in every.items():
            with self.subTest(setting=name):
                self.assertEqual(namespace[name], value)


class StartingFromTheRealSample(WriterTestCase):
    """settings.conf.sample is what an operator copies, so it is the file this
    will actually be editing. Anything that works on a three-line fixture and
    not on the real one does not work."""

    def setUp(self):
        super().setUp()
        with io.open(SAMPLE, encoding="utf-8") as handle:
            self.before = self.write(handle.read())

    def test_a_save_changes_only_the_lines_it_sets(self):
        self.save({"MAX_DCC_SLOTS": 9, "DEBUG_MODE": True, "NICKNAME": "DCCoreWin"})
        after = self.read()

        changed = [(a, b) for a, b in zip(self.before.split("\n"), after.split("\n"))
                   if a != b]
        self.assertEqual(len(changed), 3, "changed more than the three: %r" % changed)
        self.assertEqual(len(after.split("\n")), len(self.before.split("\n")),
                         "the file changed length, so something was appended")

    def test_nothing_is_appended_because_every_setting_is_already_there(self):
        """The sample lists all of them, which is why the appending path is the
        rare one rather than the usual one."""
        report = self.save(self.every_setting())

        self.assertEqual(report["added"], [])

    def test_the_explanations_above_each_setting_survive(self):
        comments_before = self.before.count("#")

        self.save({"MAX_DCC_SLOTS": 9})

        # One "#" fewer: the one that was commenting out MAX_DCC_SLOTS.
        self.assertEqual(self.read().count("#"), comments_before - 1)


class TellingTheOperatorAboutAdminConfig(WriterTestCase):
    """config.py applies admin_config.py first and settings.conf second, so
    saving here is what takes effect from now on. An operator who keeps their
    real values in the Python file should hear that at the moment it stops
    being true, not the next time they edit it and nothing happens."""

    def test_a_setting_admin_config_also_sets_is_reported(self):
        module = type(sys)("admin_config")
        module.MAX_DCC_SLOTS = 3
        self.addCleanup(sys.modules.pop, "admin_config", None)
        sys.modules["admin_config"] = module
        self.write("")

        report = self.save({"MAX_DCC_SLOTS": 9, "NICKNAME": "DCCoreWin"})

        self.assertEqual(report["shadowed"], ["MAX_DCC_SLOTS"])

    def test_no_admin_config_is_not_a_problem(self):
        sys.modules.pop("admin_config", None)
        self.write("")

        report = self.save({"MAX_DCC_SLOTS": 9})

        self.assertEqual(report["shadowed"], [])


class TheWriteIsAtomic(WriterTestCase):
    """The daemon reads this file at startup and on every rehash. A reader must
    see the whole old file or the whole new one, never half of either."""

    def test_no_temporary_file_is_left_behind(self):
        self.write("")

        self.save({"MAX_DCC_SLOTS": 9})

        strays = [n for n in os.listdir(self.dir) if n.startswith(".tmp_")]
        self.assertEqual(strays, [])

    def test_the_replace_is_a_rename_within_one_directory(self):
        """Reads the writer's own source rather than restating the rule: a
        temp file made anywhere but beside the target turns the final step
        into a cross-filesystem copy, which is not atomic."""
        with io.open(os.path.join(REPO_ROOT, "settings_file.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("dir=directory", source)
        self.assertIn("os.replace(", source)

    def test_it_does_not_reach_for_db_s_copy(self):
        """db.py has the same helper, and importing it here would close a
        cycle: db imports config, and config imports this module."""
        with io.open(os.path.join(REPO_ROOT, "settings_file.py"), encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines()
                     if not line.strip().startswith("#")]

        self.assertEqual([line for line in lines if "import db" in line], [])


if __name__ == "__main__":
    unittest.main()
