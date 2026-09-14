"""Six colour codes, chosen from menus instead of typed as control bytes.

The second half of the beta request that asked for "select colors/symbols from
drop down menu and show preview of the messages". The preview shipped first,
because a menu of sixteen colours is only worth having once you can see what
it does.

WHAT WAS ACTUALLY WRONG, and it was not only ergonomics. config holds the
DECODED bytes - a role of "\\x0313" is four characters, two of them the 0x03
control code - and the settings page was handed them raw. A browser text input
cannot display 0x03, so the field for that accent read `13`. The code was
there, invisible, and what the operator could see was not what was set:
retyping what they read would have put a literal "13" into every advert.

The same value went the other way too. Saving from the dashboard wrote the raw
control byte into settings.conf - a file people edit by hand, where a 0x03 is
invisible in an editor and is exactly what an editor strips on save.

SO THE VALUE NOW CROSSES AS THE ESCAPE TEXT settings.conf.sample has always
documented, in both directions, and encode_irc_escapes() is the half of the
round trip that was missing. decode_irc_escapes() has been there since #170's
follow-up; nothing turned it back.

WHAT THE MENUS WILL NOT DO. A value the two dropdowns cannot express - bold in
the middle of it, a code past fifteen, anything hand-written - keeps its text
box and says why. Rewriting an operator's own value into the nearest thing a
dropdown can say is the one behaviour a picker must never have.
"""

import io
import os
import re
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import theme  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

COLOUR = "\x03"
BOLD = "\x02"


def read(name):
    with io.open(os.path.join(REPO_ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


class TheWayBackOutOfAControlByte(unittest.TestCase):

    def test_a_colour_code_becomes_something_an_editor_can_hold(self):
        self.assertEqual(settings_file.encode_irc_escapes(COLOUR + "13"),
                         "\\x0313")

    def test_a_pair_keeps_its_comma(self):
        self.assertEqual(settings_file.encode_irc_escapes(COLOUR + "04,05"),
                         "\\x0304,05")

    def test_every_control_character_is_escaped_not_only_the_themed_ones(self):
        """Deciding today which codes a future theme may want is how the set
        ends up short."""
        for code in list(range(0x00, 0x20)) + [0x7f]:
            with self.subTest(code=code):
                self.assertEqual(settings_file.encode_irc_escapes(chr(code)),
                                 "\\x%02x" % code)

    def test_ordinary_text_is_left_alone(self):
        for text in ("", "plain", "a, b", "C:\\some\\path", "\\x0313"):
            with self.subTest(text=text):
                self.assertEqual(settings_file.encode_irc_escapes(text), text)

    def test_it_is_the_decoder_run_backwards(self):
        for value in (COLOUR + "13", COLOUR + "04,05", "", BOLD + "x" + "\x0f",
                      "text " + COLOUR + "07 more"):
            with self.subTest(value=value):
                there = settings_file.encode_irc_escapes(value)
                self.assertEqual(settings_file.decode_irc_escapes(there), value)

    def test_one_value_gets_one_spelling(self):
        """The decoder takes either case. Two spellings of one colour would
        each read as an edit of the other, and the settings page compares
        against a baseline string."""
        self.assertEqual(settings_file.decode_irc_escapes("\\x0F"),
                         settings_file.decode_irc_escapes("\\x0f"))
        self.assertEqual(
            settings_file.encode_irc_escapes(
                settings_file.decode_irc_escapes("\\x0F")),
            "\\x0f")

    def test_running_it_twice_changes_nothing(self):
        once = settings_file.encode_irc_escapes(COLOUR + "13")

        self.assertEqual(settings_file.encode_irc_escapes(once), once)


class WhatTheSettingsPageIsGiven(DCCoreTestCase):

    def field(self, value):
        self.set_config(CUSTOM_THEME_ACCENT=value)
        return webserver._settings_field("CUSTOM_THEME_ACCENT", str, value)

    def test_the_colour_arrives_as_something_the_page_can_show(self):
        self.assertEqual(self.field(COLOUR + "13")["value"], "\\x0313")

    def test_and_it_is_marked_as_a_colour(self):
        self.assertTrue(self.field(COLOUR + "13")["irc_colour"])

    def test_an_unset_role_is_empty_rather_than_none(self):
        """It becomes the value of a text input either way, and "None" is a
        colour code nobody typed."""
        self.assertEqual(self.field(None)["value"], "")

    def test_no_control_character_crosses(self):
        """The bug this replaces, stated as a property: whatever the role
        holds, what the page is handed is text a browser can display."""
        for value in (COLOUR + "13", COLOUR + "04,05", BOLD, "\x0f"):
            with self.subTest(value=value):
                shown = self.field(value)["value"]
                self.assertTrue(all(ch.isprintable() for ch in shown), shown)

    def test_a_setting_that_is_not_a_colour_is_untouched(self):
        self.set_config(NICKNAME="SomeBot")
        field = webserver._settings_field("NICKNAME", str, "SomeBot")

        self.assertNotIn("irc_colour", field)
        self.assertEqual(field["value"], "SomeBot")

    def test_every_role_gets_the_treatment(self):
        """Guard on the guard: six roles, and the branch keys off the name
        prefix rather than a list that could fall behind."""
        for role in theme.ROLES:
            name = f"CUSTOM_THEME_{role.upper()}"
            with self.subTest(role=role):
                self.assertTrue(
                    webserver._settings_field(name, str, COLOUR + "07")
                             .get("irc_colour"))


class TheValueSurvivesTheRoundTrip(DCCoreTestCase):
    """Page -> settings.conf -> config -> page, which is where the old
    behaviour lost it."""

    def saved(self, posted):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "settings.conf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("NICKNAME = SomeBot\n")
        settings_file.save(vars(config), {"CUSTOM_THEME_ACCENT": posted},
                           path=path, log=lambda *a, **k: None)
        with io.open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_file_holds_the_escape_text_the_sample_documents(self):
        self.assertIn("CUSTOM_THEME_ACCENT = \\x0313", self.saved("\\x0313"))

    def test_the_file_stays_a_file_a_person_can_edit(self):
        """A raw 0x03 in settings.conf is invisible in an editor, and the
        kind of thing an editor drops on save."""
        written = self.saved("\\x0304,05")

        for ch in written:
            self.assertTrue(ch.isprintable() or ch in "\r\n\t", repr(ch))

    def test_and_it_reads_back_as_the_byte_the_channel_needs(self):
        self.assertEqual(settings_file.coerce(
            "CUSTOM_THEME_ACCENT", "\\x0304,05", "", str), COLOUR + "04,05")

    def test_the_page_would_show_the_same_thing_it_sent(self):
        """The full loop. A picker that cannot land on its own output would
        mark a field dirty the moment the page was reopened."""
        posted = "\\x0304,05"
        stored = settings_file.coerce("CUSTOM_THEME_ACCENT", posted, "", str)
        self.set_config(CUSTOM_THEME_ACCENT=stored)

        shown = webserver._settings_field(
            "CUSTOM_THEME_ACCENT", str, stored)["value"]

        self.assertEqual(shown, posted)


class AControlCharacterCannotHideBehindTheEscape(DCCoreTestCase):
    """#436: the escape text is how a colour crosses settings.conf safely -
    but "\\x0a" and "\\x0d" decode to a REAL newline/carriage return, which
    is exactly what the plain line-break check further down settings_file.py
    cannot see, because the ENCODED text holds no line break at all. A value
    saved this way came back out of config as a real \\n, and
    theme.blocks() interpolates one CUSTOM_THEME_* value 8-9 times into a
    single outbound line - turning one advert into nine separate IRC
    commands sent in one send(), reserving exactly one outbound_pacer slot
    for all nine.

    Exercised through the real settings_file.save() round trip, the same
    one TheValueSurvivesTheRoundTrip above uses for the safe case - not
    coerce() alone, since the defect was specifically that encode_irc_escapes()
    ran AFTER decode_irc_escapes() had already produced the byte, hiding it
    from the line-break check that runs after both.
    """

    def saved(self, name, posted):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "settings.conf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("NICKNAME = SomeBot\n")
        settings_file.save(vars(config), {name: posted},
                           path=path, log=lambda *a, **k: None)
        with io.open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_documented_newline_escape_is_refused(self):
        with self.assertRaises(settings_file.SettingsWriteError):
            self.saved("CUSTOM_THEME_BORDER", "\\x0a")

    def test_the_documented_carriage_return_escape_is_refused(self):
        with self.assertRaises(settings_file.SettingsWriteError):
            self.saved("CUSTOM_THEME_BORDER", "\\x0d")

    def test_a_null_byte_escape_is_refused_too(self):
        with self.assertRaises(settings_file.SettingsWriteError):
            self.saved("CUSTOM_THEME_BORDER", "\\x00")

    def test_the_newline_hidden_in_a_longer_value_is_still_caught(self):
        """Not just a bare "\\x0a" - the exact reported shape mixes it with
        real colour codes on either side."""
        with self.assertRaises(settings_file.SettingsWriteError):
            self.saved("CUSTOM_THEME_BORDER", "\\x0304,05\\x0a\\x0301,00")

    def test_an_ordinary_colour_still_saves(self):
        """Control: the fix must not refuse what TheValueSurvivesTheRoundTrip
        above already proved works."""
        written = self.saved("CUSTOM_THEME_BORDER", "\\x0304,05")
        self.assertIn("CUSTOM_THEME_BORDER = \\x0304,05", written)

    def test_a_value_past_the_length_cap_is_refused(self):
        """8-9 repeats of one role compete with everything else for
        announce.IRC_LINE_BUDGET - a role need not contain a control
        character to be a problem if it is simply very long."""
        with self.assertRaises(settings_file.SettingsWriteError):
            self.saved("CUSTOM_THEME_BORDER", "\\x0304,05" * 10)

    def test_the_length_cap_measures_the_decoded_bytes_not_the_escape_text(self):
        """The escape spelling is ~4x longer than the byte it decodes to -
        capping the TEXT typed would refuse ordinary multi-code values that
        are perfectly short on the wire."""
        # "\x0304,05" x3 decodes to 18 bytes on the wire (COLOUR + "04,05"
        # is 6 bytes each), comfortably under CUSTOM_THEME_MAX_BYTES (30) -
        # but its escape spelling ("\\x0304,05" x3) is 27 characters, close
        # enough to the cap that measuring the posted TEXT instead of the
        # decoded value could wrongly refuse it.
        written = self.saved("CUSTOM_THEME_BORDER", "\\x0304,05" * 3)
        self.assertIn("CUSTOM_THEME_BORDER = ", written)


class TheTemplatesCannotBeHandedARawControlCharacterEither(unittest.TestCase):
    """#436's defence in depth: settings_file.py's writer is the only
    guarded PATH IN, but theme.palette() is the one place every outbound
    template gets its colours from - a hand-edited settings.conf or
    admin_config.py answers to neither coerce() nor _check_writable()."""

    def test_a_raw_newline_in_config_does_not_reach_the_palette(self):
        painted = theme.palette({"CUSTOM_THEME_BORDER": "x\ny"})

        self.assertNotIn("\n", painted["border"])

    def test_a_raw_carriage_return_does_not_reach_the_palette(self):
        painted = theme.palette({"CUSTOM_THEME_BORDER": "x\ry"})

        self.assertNotIn("\r", painted["border"])

    def test_a_value_that_is_nothing_but_a_newline_keeps_the_preset(self):
        """Stripped to nothing, not published as an empty role - the same
        rule an absent override already follows."""
        plain = theme.palette()
        painted = theme.palette({"CUSTOM_THEME_BORDER": "\n"})

        self.assertEqual(painted["border"], plain["border"])


class WhatTheMenusCanSay(unittest.TestCase):
    """The parse rule is one regular expression in app.js, and it is the whole
    decision - so it is lifted out of the file and RUN, rather than checked
    for its spelling. Python and JavaScript agree on this pattern: anchors, an
    escaped literal, \\d with a counted repeat, a non-capturing group and a
    question mark all mean the same thing in both."""

    def rule(self):
        source = read("app.js")
        body = source.split("function parseIrcColour(", 1)[1].split("\n  }", 1)[0]
        found = re.search(r"=\s*/(.+?)/\.exec\(", body)
        self.assertTrue(found, "parseIrcColour no longer matches with a regex")
        return re.compile(found.group(1))

    def run_rule(self, text):
        """SEARCH, not match. JavaScript's .exec() is unanchored, and so is
        re.search(); re.match() anchors at position 0 whatever the pattern
        says, which quietly makes the rule's own "^" untestable - a rule that
        had lost it passed every assertion here."""
        return self.rule().search(text)

    def test_the_rule_was_found(self):
        """Guard on the guard - a rule that failed to lift would make every
        assertion below vacuous."""
        self.assertTrue(self.rule().pattern)

    def test_a_foreground_on_its_own(self):
        match = self.run_rule("\\x0304")

        self.assertTrue(match)
        self.assertEqual(match.group(1), "04")
        self.assertIsNone(match.group(2))

    def test_a_foreground_and_a_background(self):
        match = self.run_rule("\\x0304,05")

        self.assertTrue(match)
        self.assertEqual((match.group(1), match.group(2)), ("04", "05"))

    def test_one_digit_is_accepted_because_clients_write_it(self):
        self.assertTrue(self.run_rule("\\x034"))

    def test_it_is_anchored_at_both_ends(self):
        """Stated separately from the list below, because these two are what
        the anchors are FOR - and an unanchored rule would find a colour
        inside a value that is mostly something else, then throw the rest of
        it away."""
        self.assertIsNone(self.run_rule("bold \\x0304"), "not anchored at the start")
        self.assertIsNone(self.run_rule("\\x0304 bold"), "not anchored at the end")

    def test_what_it_refuses(self):
        """Each of these keeps its text box. A picker that accepted them would
        have to throw part of the value away to show it."""
        for value in ("\\x0304 text", "\\x02\\x0304", "text", "\\x03",
                      "\\x0304,", "\\x030405", " \\x0304"):
            with self.subTest(value=value):
                self.assertIsNone(self.run_rule(value), value)

    def test_a_code_past_fifteen_is_refused_by_the_function_not_the_rule(self):
        """The rule takes two digits; sixteen colours do not go past 15, and
        IRC_COLOURS[16] is undefined - a swatch painted from it would be no
        colour at all, which reads as a broken theme."""
        body = read("app.js").split("function parseIrcColour(", 1)[1] \
                             .split("\n  }", 1)[0]

        self.assertIn("> 15", body)
        self.assertIn("return null", body)


class TheControlOnThePage(unittest.TestCase):

    def function(self, name):
        return read("app.js").split("function %s(" % name, 1)[1] \
                             .split("\n  }", 1)[0]

    def test_there_are_sixteen_names_for_the_sixteen_colours(self):
        source = read("app.js")
        names = source.split("var IRC_COLOUR_NAMES = [", 1)[1].split("]", 1)[0]
        hexes = source.split("var IRC_COLOURS = [", 1)[1].split("]", 1)[0]

        self.assertEqual(len(re.findall(r'"[^"]+"', names)), 16)
        self.assertEqual(len(re.findall(r"#[0-9a-fA-F]{6}", hexes)), 16)

    def test_a_code_is_written_with_both_digits(self):
        """One value, one spelling - the same reason the encoder is lowercase.
        A field that came back "\\x034" where the baseline said "\\x0304"
        would show as an unsaved change nobody made."""
        body = self.function("formatIrcColour")

        # Both halves. Counting "pad2(" was satisfied by the background alone
        # while the foreground went out unpadded.
        self.assertIn("pad2(fg)", body)
        self.assertIn("pad2(bg)", body)

    def test_no_foreground_means_no_code_at_all(self):
        """Not a code naming the default - an empty role is how theme.py says
        "use the preset", and it has always been the empty string."""
        self.assertIn('if (fg === "") { return ""; }',
                      self.function("formatIrcColour"))

    def test_a_background_cannot_be_chosen_without_a_foreground(self):
        """There is no such code as "\\x03,05". Disabling the menu means the
        operator never assembles a value that could not be saved."""
        body = self.function("attachIrcColourPickers")

        self.assertIn('bg.disabled = fg.value === ""', body)
        self.assertIn('if (bg.disabled) { bg.value = ""; }', body)

    def test_a_value_the_menus_cannot_say_keeps_its_box(self):
        """The GUARD, not just the presence of a text box below it. A branch
        that always drew the picker still had the fallback markup sitting in
        an else nothing could reach."""
        branch = read("app.js").split("if (field.irc_colour) {", 1)[1] \
                               .split("} else if", 1)[0]
        picked, fallback = branch.split("} else {", 1)

        self.assertIn("var picked = parseIrcColour(current);", picked)
        self.assertIn("if (picked) {", picked)
        self.assertIn("ircColourPickerHtml(field.name, picked)", picked)
        self.assertIn('type="text"', fallback)
        self.assertNotIn("ircColourPickerHtml", fallback,
                         "the fallback draws the picker it exists to avoid")

    def test_and_says_so_rather_than_looking_like_an_ordinary_field(self):
        branch = read("app.js").split("if (field.irc_colour) {", 1)[1] \
                               .split("} else if", 1)[0]

        self.assertIn("menus cannot offer", branch)

    def test_the_two_menus_are_invisible_to_the_ordinary_listener_pass(self):
        """They carry data-irc-part, not data-setting: two selects mean one
        setting between them, and neither one's value is the value to
        record."""
        picker = self.function("ircColourPickerHtml")

        self.assertIn('data-irc-part="fg"', picker)
        self.assertIn('data-irc-part="bg"', picker)
        self.assertNotIn("data-setting", picker)

    def test_the_swatch_takes_its_colours_from_the_table(self):
        body = self.function("paintIrcSwatch")

        self.assertIn("IRC_COLOURS[Number(fg)]", body)
        self.assertIn("IRC_COLOURS[Number(bg)]", body)

    def test_the_picker_is_wired_after_every_render(self):
        source = read("app.js")
        after_insert = source.split("el.settingsFields.innerHTML = html;", 1)[1]

        self.assertIn("attachIrcColourPickers()",
                      after_insert.split("\n  }", 1)[0])


class OnePlaceRecordsAnEdit(unittest.TestCase):
    """The picker cannot go through onSettingsFieldChange - it has two
    controls and one value - so the part after the value is computed was
    lifted out. That part is where the easy mistake lives."""

    def function(self, name):
        return read("app.js").split("function %s(" % name, 1)[1] \
                             .split("\n  }", 1)[0]

    def test_a_value_put_back_as_it_was_found_is_not_an_edit(self):
        """Deleted from the dirty set, not stored - or the save bar counts a
        field the operator has undone."""
        body = self.function("recordSettingChange")

        self.assertIn("delete state.settingsDirty[name]", body)

    def test_both_controls_go_through_it(self):
        source = read("app.js")

        self.assertIn("recordSettingChange(name, newValue, input)",
                      self.function("onSettingsFieldChange"))
        self.assertIn("recordSettingChange(name, formatIrcColour(",
                      self.function("attachIrcColourPickers"))

    def test_the_old_body_is_not_still_there_as_well(self):
        """A copy left behind in the original handler would be the drift this
        extraction exists to prevent."""
        source = read("app.js")

        self.assertEqual(source.count("delete state.settingsDirty[name]"), 1)
        self.assertEqual(source.count("state.settingsDirty[name] = newValue"), 1)


class TheSwatchTakesNoColourOfItsOwn(unittest.TestCase):
    """The sixteen belong to IRC and live in one place. A copy of them in the
    stylesheet would be a second place to keep right - and the panel's frame
    still belongs to the dashboard theme."""

    def rules(self):
        css = read("style.css")
        return [block for block in css.split("}")
                if ".irc-colour" in block.split("{", 1)[0]]

    def test_the_rules_exist(self):
        self.assertTrue(self.rules(), "the picker has no styling at all")

    def test_none_of_them_names_a_colour(self):
        for block in self.rules():
            selector = block.split("{", 1)[0].strip()
            with self.subTest(rule=selector):
                self.assertIsNone(
                    re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block),
                    "%s paints a colour the stylesheet should not know" % selector)

    def test_the_frame_comes_from_the_dashboard_palette(self):
        self.assertIn("var(--border", " ".join(self.rules()))


if __name__ == "__main__":
    unittest.main()
