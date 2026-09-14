"""The preview renders the colour, not the nine characters that name it.

Issue #367, reported with a screenshot: picking a custom colour from the new
dropdown put a literal, unstyled `\\x0309,07` at the front of the advert sample
and pushed the box off to the right. Every role still on "Theme default"
rendered correctly, which is what isolated it to values that had been through
the picker.

WHY IT HAPPENED. A colour crosses the wire as the escape TEXT - nine
characters, `\\x0309,07` - because a browser text input cannot hold the 0x03
control byte and settings.conf should not carry one either. theme.palette()
deals in the decoded byte, which is what theme.CLASSIC and every other preset
holds. The save path already bridged the two: settings_file.coerce() decodes
on the way in. The preview path did not, so it handed nine literal characters
to a builder expecting a colour code, and renderIrcLine() - which looks for a
real 0x03 - found none and drew them as text.

THE SAVE PATH WAS NOT AFFECTED, and the report's second claim, that a saved
value would reach the channel as visible text, does not hold: _check_writable()
coerces before writing and config ends up with the real byte. Checked before
fixing anything, because the two paths look alike and only one of them was
wrong. A test below pins that down so the difference cannot be argued about
again.

WHY THE EXISTING TESTS MISSED IT, which the report also got right: the
picker's own round trip is `parseIrcColour(formatIrcColour(...))`, and both
sides speak the escape text. It is perfectly self-consistent and can never
surface the mismatch. What was missing is an assertion against the REAL byte -
against what theme.CLASSIC actually contains - which is what this file adds.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import settings_file  # noqa: E402
import theme  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

# Built from chr() so that no escape processing, in this file or in any tool
# that edits it, can quietly turn the text into the byte - which is the exact
# confusion under test.
BACKSLASH = chr(92)
AS_TYPED = BACKSLASH + "x0309,07"      # nine characters, what the page posts
AS_MEANT = "\x0309,07"                 # six, what a theme preset holds


class TheTwoFormsAreDifferentThings(unittest.TestCase):
    """Stated first, because every assertion below depends on it and the two
    are one character apart in any listing that shows them."""

    def test_the_typed_form_is_text(self):
        self.assertEqual(len(AS_TYPED), 9)
        self.assertNotIn("\x03", AS_TYPED)

    def test_the_meant_form_is_a_control_code(self):
        self.assertEqual(len(AS_MEANT), 6)
        self.assertTrue(AS_MEANT.startswith("\x03"))

    def test_a_shipped_preset_holds_the_second(self):
        """theme.CLASSIC is the reference for what a role value IS."""
        for role, value in theme.CLASSIC.items():
            with self.subTest(role=role):
                self.assertNotIn(BACKSLASH, value)
                self.assertTrue(value.startswith("\x03"))


class WhatThePreviewIsGiven(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(THEME="classic", NICKNAME="SomeBot",
                        CHANNEL="#somechannel")

    def test_the_typed_text_becomes_the_code_it_names(self):
        wanted = webserver.theme_preview_overrides(
            {"CUSTOM_THEME_BORDER": AS_TYPED})

        self.assertEqual(wanted["CUSTOM_THEME_BORDER"], AS_MEANT)

    def test_a_role_reaching_the_palette_looks_like_a_preset_role(self):
        """The property, not the one value: whatever the operator picked, what
        theme.palette() gets must be the same KIND of string theme.CLASSIC
        holds, or the builders concatenate text into an outbound line."""
        wanted = webserver.theme_preview_overrides(
            {"CUSTOM_THEME_BORDER": AS_TYPED,
             "CUSTOM_THEME_ACCENT": BACKSLASH + "x0313"})
        roles = theme.palette(wanted)

        for role, value in roles.items():
            with self.subTest(role=role):
                self.assertNotIn(BACKSLASH, value,
                                 "%s reaches the palette as text" % role)

    def test_an_already_decoded_value_is_not_mangled(self):
        """Idempotent, so a caller that has already decoded - or a preset name
        arriving unchanged - is not decoded twice into something else."""
        wanted = webserver.theme_preview_overrides(
            {"CUSTOM_THEME_BORDER": AS_MEANT})

        self.assertEqual(wanted["CUSTOM_THEME_BORDER"], AS_MEANT)

    def test_a_non_string_is_still_dropped_rather_than_coerced(self):
        wanted = webserver.theme_preview_overrides({"CUSTOM_THEME_BORDER": 7})

        self.assertEqual(wanted["CUSTOM_THEME_BORDER"], "")


class TheSampleTheOperatorSees(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(THEME="classic", NICKNAME="SomeBot",
                        CHANNEL="#somechannel")

    def preview(self, **picked):
        return webserver.build_theme_preview(
            webserver.theme_preview_overrides(picked))

    def test_no_line_carries_the_escape_text(self):
        """The screenshot, as an assertion. renderIrcLine() looks for a real
        0x03 and draws anything else as characters, so a backslash surviving
        this far IS the visible prefix that was reported."""
        preview = self.preview(CUSTOM_THEME_BORDER=AS_TYPED,
                               CUSTOM_THEME_ACCENT=BACKSLASH + "x0313")

        for name in ("advert", "notice"):
            with self.subTest(line=name):
                self.assertNotIn(BACKSLASH, preview[name])

    def test_the_picked_colour_is_actually_in_the_line(self):
        """Guard on the guard: a preview that dropped the override entirely
        would also contain no backslash."""
        preview = self.preview(CUSTOM_THEME_BORDER=AS_TYPED)

        self.assertIn(AS_MEANT, preview["advert"])

    def test_and_the_default_roles_are_untouched_beside_it(self):
        """What isolated the bug in the report: the roles left at "Theme
        default" rendered correctly while the picked one did not."""
        preview = self.preview(CUSTOM_THEME_BORDER=AS_TYPED)

        self.assertIn(theme.CLASSIC["separator"], preview["advert"])


class ThePreviewAgreesWithTheSave(DCCoreTestCase):
    """The preview's whole claim is that it shows what saving would produce.
    Two functions that merely happen to agree today are two functions that can
    stop agreeing - which is what happened - so this asserts the agreement
    directly rather than asserting each side's spelling."""

    def setUp(self):
        super().setUp()
        self.set_config(THEME="classic")

    def test_the_same_posted_value_reaches_both_as_the_same_thing(self):
        previewed = webserver.theme_preview_overrides(
            {"CUSTOM_THEME_BORDER": AS_TYPED})["CUSTOM_THEME_BORDER"]
        saved = settings_file.coerce("CUSTOM_THEME_BORDER", AS_TYPED, "", str)

        self.assertEqual(previewed, saved)

    def test_the_save_path_was_never_the_broken_one(self):
        """The report's second claim. It does not hold, and the difference
        matters: a bug in this path would have been reaching real channels."""
        saved = settings_file.coerce("CUSTOM_THEME_BORDER", AS_TYPED, "", str)

        self.assertEqual(saved, AS_MEANT)
        self.assertNotIn(BACKSLASH, saved)

    def test_and_the_file_still_holds_the_readable_form(self):
        """Which is the whole reason the two forms exist. Fixing the preview
        must not have been done by making the page post control bytes."""
        self.assertEqual(settings_file.encode_irc_escapes(AS_MEANT), AS_TYPED)

    def test_an_outbound_line_built_from_it_carries_no_text(self):
        """The end of the trip: what a channel would actually receive."""
        line = announce.build_advert_line(
            "#somechannel", "SomeBot", "1", "1MB", "Sep 7th", "1/1", "0",
            "1MB/s", "1MB/s", "1 File", "v1",
            settings={"CUSTOM_THEME_BORDER":
                      settings_file.coerce("CUSTOM_THEME_BORDER", AS_TYPED,
                                           "", str)})

        self.assertNotIn(BACKSLASH, line)
        self.assertIn(AS_MEANT, line)


class ThePageStillSpeaksTheReadableForm(unittest.TestCase):
    """The fix is on the server. Changing the page to emit a raw control byte
    would have "fixed" the preview by undoing the reason the picker exists -
    a text input cannot display 0x03, which is what made the settings box read
    `13` for a value of `\\x0313` in the first place."""

    def app_js(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_the_picker_still_builds_the_escape_text(self):
        source = self.app_js()
        body = source.split("function formatIrcColour(", 1)[1].split("\n  }", 1)[0]

        self.assertIn(BACKSLASH * 2 + "x03", body)

    def test_and_still_reads_it_back(self):
        source = self.app_js()
        body = source.split("function parseIrcColour(", 1)[1].split("\n  }", 1)[0]

        self.assertIn(BACKSLASH * 2 + "x03", body)


if __name__ == "__main__":
    unittest.main()
