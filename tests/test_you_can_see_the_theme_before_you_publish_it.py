"""What the channel will see, on the page where the colours are chosen.

Asked for during the RC1 beta: "bot font settings shows become easier. select
colors/symbols from drop down menu and show preview of the messages". This is
the preview half.

WHAT IT REPLACED. The six CUSTOM_THEME_* settings hold raw mIRC colour codes,
typed into text boxes. Finding out what a change did meant saving it,
rehashing, and waiting for the next advert - up to ANNOUNCE_INTERVAL away, on a
bot other people are using. The channel was the preview.

TWO SAMPLES, AND THAT IS NOT A PRESENTATION CHOICE. Between them the advert and
the completion notice use all six roles, and neither uses all six alone: the
advert never touches `accent`. One sample would leave one setting looking as
though it did nothing, which is exactly the confusion a preview exists to end.

BUILT BY THE REAL BUILDERS. announce.build_advert_line() and
build_transfer_complete_line() were lifted out of the advert loop and the
notice for this, and both callers now go through them. A preview built from a
copy of those templates would drift the first time one changed, and would then
lie with a straight face.

AND IT NEVER TOUCHES CONFIG. The point is the colour typed and not yet saved,
so the pending values are passed down to theme.palette() as an override
mapping. A live daemon is serving a channel while somebody is choosing
colours; the preview must not write to what every other thread is reading.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import theme  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

COLOUR = "\x03"


class AskingWhatItWouldLookLike(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(THEME="classic", NICKNAME="SomeBot",
                        CHANNEL="#somechannel")
        for role in theme.ROLES:
            self.set_config(**{f"CUSTOM_THEME_{role.upper()}": None})

    def test_the_palette_can_be_asked_about_a_theme_it_is_not_using(self):
        classic = theme.palette()
        forest = theme.palette({"THEME": "forest"})

        self.assertNotEqual(classic, forest)
        self.assertEqual(theme.palette(), classic, "config was changed")

    def test_one_role_can_be_overridden_on_top(self):
        painted = theme.palette({"CUSTOM_THEME_ACCENT": COLOUR + "13"})

        self.assertEqual(painted["accent"], COLOUR + "13")
        self.assertEqual(painted["border"], theme.palette()["border"],
                         "overriding one role changed another")

    def test_an_unknown_theme_name_is_ignored(self):
        """Rather than silently previewing the configured one under a name the
        operator did not choose."""
        self.assertEqual(theme.palette({"THEME": "no-such-theme"}),
                         theme.palette())

    def test_nothing_reaches_the_live_config(self):
        before = {role: getattr(config, f"CUSTOM_THEME_{role.upper()}", None)
                  for role in theme.ROLES}

        theme.palette({"THEME": "orchid", "CUSTOM_THEME_ACCENT": COLOUR + "04"})

        after = {role: getattr(config, f"CUSTOM_THEME_{role.upper()}", None)
                 for role in theme.ROLES}
        self.assertEqual(before, after)
        self.assertEqual(config.THEME, "classic")


class BothSamplesAreRendered(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(THEME="classic", NICKNAME="SomeBot",
                        CHANNEL="#somechannel")

    def test_there_are_two_of_them(self):
        preview = webserver.build_theme_preview()

        self.assertTrue(preview["advert"])
        self.assertTrue(preview["notice"])

    def test_between_them_every_role_appears(self):
        """The reason there are two. Neither sample uses all six roles, so a
        setting missing from both would look like it does nothing."""
        preview = webserver.build_theme_preview()
        shown = preview["advert"] + preview["notice"]
        roles = theme.palette()

        for role, code in roles.items():
            with self.subTest(role=role):
                self.assertIn(code, shown,
                              f"{role} appears in neither sample, so changing "
                              f"it would look like it did nothing")

    def test_the_accent_is_the_one_only_the_notice_shows(self):
        """Stated outright, because it is the whole argument for two samples
        and a later edit could quietly drop the second."""
        preview = webserver.build_theme_preview()
        accent = theme.palette()["accent"]

        self.assertIn(accent, preview["notice"])
        self.assertNotIn(accent, preview["advert"])

    def test_the_protocol_envelope_is_not_shown(self):
        """"PRIVMSG #chan :" is how the line travels, not what anybody sees in
        a channel."""
        preview = webserver.build_theme_preview()

        self.assertNotIn("PRIVMSG", preview["advert"])
        self.assertNotIn("PRIVMSG", preview["notice"])

    def test_no_line_ending_survives_into_the_page(self):
        preview = webserver.build_theme_preview()

        for line in (preview["advert"], preview["notice"]):
            self.assertNotIn("\r", line)
            self.assertNotIn("\n", line)

    def test_a_pending_change_is_what_is_rendered(self):
        """The whole point: the colour typed and not yet saved."""
        plain = webserver.build_theme_preview()
        painted = webserver.build_theme_preview(
            {"CUSTOM_THEME_ACCENT": COLOUR + "13"})

        self.assertNotEqual(plain["notice"], painted["notice"])
        self.assertIn(COLOUR + "13", painted["notice"])

    def test_a_pending_change_reaches_the_advert_as_well(self):
        """The accent test above can only speak for the notice, because the
        accent is the one role the advert does not use. Without this, an advert
        builder that dropped its override on the floor would still pass."""
        plain = webserver.build_theme_preview()
        painted = webserver.build_theme_preview(
            {"CUSTOM_THEME_BORDER": COLOUR + "13"})

        self.assertNotEqual(plain["advert"], painted["advert"])
        self.assertIn(COLOUR + "13", painted["advert"])

    def test_the_sample_figures_do_not_move(self):
        """Reading the live counters would make the preview flicker as
        transfers come and go, and a sample that changes while you are
        comparing two colours is a sample you cannot compare."""
        first = webserver.build_theme_preview()
        second = webserver.build_theme_preview()

        self.assertEqual(first, second)

    def test_it_is_the_real_builder(self):
        """Not a copy of the template. A copy drifts the first time one of
        them changes, and then the preview lies."""
        preview = webserver.build_theme_preview()
        directly = announce.build_advert_line(
            "#somechannel", "SomeBot", "719,041", "5.48 TB", "Sep 7th", "3/3",
            "0", "2.4MB/s", "24.7MB/s", "1,204 Files (8.9 TB)",
            config.SCRIPT_VERSION)

        self.assertIn(preview["advert"], directly)


class WhatMaySteerAPreview(DCCoreTestCase):
    """It reaches theme.palette(); everything else on the Settings page has
    nothing to say about colour."""

    def test_the_theme_and_the_six_roles_pass(self):
        wanted = webserver.theme_preview_overrides(
            {"THEME": "forest", "CUSTOM_THEME_ACCENT": COLOUR + "13"})

        self.assertEqual(wanted,
                         {"THEME": "forest", "CUSTOM_THEME_ACCENT": COLOUR + "13"})

    def test_nothing_else_does(self):
        wanted = webserver.theme_preview_overrides(
            {"NICKNAME": "somethingelse", "WEBUI_PORT": 1,
             "ADMIN_PASSWORD_HASH": "x"})

        self.assertEqual(wanted, {})

    def test_an_unknown_theme_is_dropped_rather_than_passed_on(self):
        self.assertEqual(webserver.theme_preview_overrides({"THEME": "../etc"}),
                         {})

    def test_a_non_string_role_becomes_empty_rather_than_itself(self):
        """It is concatenated into an outbound line by the builders."""
        wanted = webserver.theme_preview_overrides({"CUSTOM_THEME_ACCENT": 7})

        self.assertEqual(wanted["CUSTOM_THEME_ACCENT"], "")

    def test_nothing_at_all_is_not_an_error(self):
        self.assertEqual(webserver.theme_preview_overrides(None), {})
        self.assertEqual(webserver.theme_preview_overrides("not an object"), {})


class ThePageRendersTheCodes(unittest.TestCase):

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    # The end of a top-level function in app.js: a closing brace at two
    # spaces. Every function in that file is nested one level inside the
    # module IIFE, so nothing else in a body sits at that indent.
    END = "\n  }"

    def function(self, source, name):
        return source.split("function %s(" % name, 1)[1].split(self.END, 1)[0]

    def after(self, source, anchor):
        return source.split(anchor, 1)[1].split(self.END, 1)[0]

    def body(self):
        return self.source().split("function renderIrcLine(", 1)[1] \
                            .split("\n  }", 1)[0]

    def test_it_handles_the_three_codes_the_themes_use(self):
        body = self.body()

        self.assertIn("\\u0003", body)   # colour
        self.assertIn("\\u0002", body)   # bold
        self.assertIn("\\u000f", body)   # reset

    def test_a_background_is_read_after_the_comma(self):
        self.assertIn('=== ","', self.body())

    def test_the_text_is_escaped(self):
        """A sample is built from config the operator typed, and rendered with
        innerHTML so the colour spans work."""
        self.assertIn("escapeHtml(ch)", self.body())

    def test_every_colour_pushed_into_a_style_is_an_index_into_the_table(self):
        """Not "the table is mentioned somewhere". A code off the wire that
        reached a style attribute as text would be an injection point, and the
        table is exactly what stops it - so check each push, not the file."""
        pushes = re.findall(r'style\.push\("(?:background-)?color:" \+ ([^)]+)\)',
                            self.body())

        self.assertTrue(pushes, "nothing pushes a colour - has the renderer gone?")
        for expression in pushes:
            self.assertTrue(expression.strip().startswith("IRC_COLOURS["),
                            "a style takes its colour from %s, not from the "
                            "table" % expression.strip())

    def test_the_codes_are_read_as_numbers(self):
        self.assertIn("parseInt(", self.body())

    def test_the_panel_is_drawn_when_the_category_opens(self):
        """It is inserted saying "Loading" and the refresh otherwise runs only
        on an edit - so opening the category and changing nothing would leave
        it there."""
        # AFTER the markup lands. There are two `appearance` branches in
        # renderSettingsCategory - one builds the panel into the html string,
        # the other fills it once that string is in the document - and slicing
        # to the first found the markup, which cannot call anything.
        source = self.source()
        after_insert = source.split("el.settingsFields.innerHTML = html;", 1)[1]
        opened = after_insert.split("\n  }", 1)[0]

        self.assertIn('category.id === "appearance"', opened)
        self.assertIn("refreshThemePreview()", opened)

    def test_and_again_on_every_edit(self):
        """`function refreshThemePreview()` contains the same text as a call to
        it, so counting the bare name let the definition stand in for one of
        the two call sites."""
        calls = re.findall(r"(?<!function )refreshThemePreview\(\)",
                           self.source())

        self.assertGreaterEqual(len(calls), 2,
                                "the sample is drawn once and then never "
                                "again as the colours are typed")

    def test_the_second_call_site_is_the_one_every_edit_runs_through(self):
        """Named rather than counted. The other call site is the category
        opening, and a sample that only draws on open is not a preview of what
        you are typing - so say WHERE the second one is: the save bar, which
        every changed field updates."""
        source = self.source()
        save_bar = self.function(source, "updateSettingsSaveBar")

        self.assertIn("refreshThemePreview()", save_bar)
        recorded = self.after(source, "state.settingsDirty[name] = newValue;")
        self.assertIn("updateSettingsSaveBar();", recorded,
                      "recording an edit no longer reaches the save bar")

    def test_it_sends_what_is_on_screen_not_what_is_saved(self):
        body = self.source().split("function refreshThemePreview(", 1)[1] \
                            .split("\n  }", 1)[0]

        self.assertIn("state.settingsDirty", body)
        self.assertIn("/api/settings/theme-preview", body)


class TheOutboundTextDidNotChange(DCCoreTestCase):
    """The builders were lifted out of two live paths. Whatever else this
    feature does, the channel must see exactly what it saw before."""

    def test_the_advert_still_carries_its_fields(self):
        line = announce.build_advert_line(
            "#c", "Bot", "1,234", "5GB", "Sep 7th", "3/3", "0", "1MB/s",
            "2MB/s", "9 Files", "v1")

        for part in ("PRIVMSG #c :", "Type: ", "@Bot", "For My List Of: ",
                     "1,234", "Files (5GB)", "created ", "Sep 7th",
                     "Slots: 3/3", "Queued: 0", "Speed: 1MB/s / Record: 2MB/s",
                     "Total Sent: 9 Files", "Search: ", "ON", "v1"):
            self.assertIn(part, line, part)
        self.assertTrue(line.endswith("\r\n"))

    def test_the_notice_still_carries_its_fields(self):
        line = announce.build_transfer_complete_line(
            "#c", "someone", "A.flac", "9 Files", "1", "2", "3:04 pm", "1MB/s")

        for part in ("PRIVMSG #c :", "Sent", "A.flac", "To: ", "someone",
                     "Total Sent: ", "9 Files", "Yesterday: ", "Today: ",
                     "[as of 3:04 pm]", "Speed: ", "1MB/s"):
            self.assertIn(part, line, part)
        self.assertTrue(line.endswith("\r\n"))

    def test_both_callers_go_through_the_builders(self):
        """A second copy of either template would be the drift this was
        extracted to prevent."""
        with io.open(os.path.join(REPO_ROOT, "announce.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        # #434: the live call site now routes through fit_irc_line() too, so
        # the literal assignment is "announce_msg = fit_irc_line(" with
        # build_advert_line( called from inside its lambda - checked as two
        # separate counts rather than one combined string, so either half
        # drifting back apart is still caught.
        self.assertEqual(source.count("announce_msg = fit_irc_line("), 1)
        self.assertEqual(source.count("lambda ts: build_advert_line("), 1)
        self.assertEqual(source.count("return build_transfer_complete_line("), 1)
        self.assertEqual(len(re.findall(r'f"PRIVMSG \{chan\w*\} :"', source)), 2,
                         "a template was copied rather than called")


if __name__ == "__main__":
    unittest.main()
