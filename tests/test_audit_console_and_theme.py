r"""Two settings that told the operator something untrue.

Both come out of the sweep over the files no audit lens was pointed at, and
both are the same shape: the code and the documentation disagreed, and the
documentation was the half an operator acts on.

THE CONSOLE SAID OFF WHILE IT WAS ON

WEBUI_CONSOLE_ENABLED is declared `bool = None`, and None does not mean False:
console_is_enabled() reads it as "on while the dashboard is loopback-only". A
stock install therefore has the Console - ban, unban, clearqueue, rehash,
update, behind the dashboard password alone - switched ON.

The Settings page renders a bool from `!!value`, so None drew an unchecked box.
The operator was told the remote admin console was disabled while it was live.

THE THEME OVERRIDES COULD NOT BE EXPRESSED AT ALL

settings.conf.sample documents each CUSTOM_THEME_* override as "a raw mIRC
code string like \x0306,06", and defaults.py says the same. coerce() returned
the text verbatim, so those nine literal characters went into every advert,
every "Sent:" notice and every search header - broadcast on a five-minute
cycle, with no error anywhere.

There was no workaround either. A colour code starts with 0x03, a control
character: settings.conf is edited in a text editor and the dashboard field is
a browser text input, and neither can produce that byte. Typing the escape was
the only route, and it was the one that did not work.
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
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheConsoleFieldSaysWhatIsActuallyHappening(DCCoreTestCase):

    def field(self):
        return webserver._settings_field("WEBUI_CONSOLE_ENABLED", bool,
                                         getattr(config, "WEBUI_CONSOLE_ENABLED", None))

    def test_an_unset_console_on_loopback_is_reported_as_on(self):
        self.set_config(WEBUI_CONSOLE_ENABLED=None, WEBUI_HOST="127.0.0.1")

        note = self.field().get("note", "")

        self.assertIn("ON", note)
        self.assertTrue(webserver.console_is_enabled(),
                        "the fixture no longer reproduces the tri-state")

    def test_an_unset_console_on_the_lan_is_reported_as_off(self):
        self.set_config(WEBUI_CONSOLE_ENABLED=None, WEBUI_HOST="0.0.0.0")

        note = self.field().get("note", "")

        self.assertIn("OFF", note)
        self.assertFalse(webserver.console_is_enabled())

    def test_the_value_itself_is_left_unset(self):
        """A note, not a corrected value. Sending the EFFECTIVE value would
        make the checkbox truthful and then have the next save write an
        explicit True - so an operator who later moved the dashboard onto the
        LAN would keep a Console that should have switched itself off."""
        self.set_config(WEBUI_CONSOLE_ENABLED=None, WEBUI_HOST="127.0.0.1")

        self.assertIsNone(self.field()["value"])

    def test_an_explicit_choice_gets_no_note(self):
        """Somebody who has made the choice does not need it explained, and a
        note beside a checkbox that already matches would just be noise."""
        for chosen in (True, False):
            with self.subTest(chosen=chosen):
                self.set_config(WEBUI_CONSOLE_ENABLED=chosen,
                                WEBUI_HOST="127.0.0.1")

                self.assertNotIn("note", self.field())

    def test_no_other_setting_grows_a_note(self):
        """The note exists for a tri-state. Every other bool means what it
        says."""
        field = webserver._settings_field("WEBUI_OPEN_BROWSER", bool, None)

        self.assertNotIn("note", field)

    def test_the_page_renders_the_note_as_text(self):
        """escapeHtml() encodes & < > but NOT quotes, so a value that reaches
        an ATTRIBUTE is an injection risk. This one is text content, which is
        what escapeHtml is correct for - pinned because the distinction is the
        whole reason app.js has that comment."""
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn('escapeHtml(field.note)', source)
        self.assertNotIn('data-note="' , source)


class AThemeOverrideCanBeTyped(DCCoreTestCase):

    def coerce(self, raw):
        return settings_file.coerce("CUSTOM_THEME_BORDER", raw, None, str)

    def test_the_documented_form_becomes_a_real_control_code(self):
        value = self.coerce("\\x0306,06")

        self.assertEqual(value, chr(3) + "06,06")
        self.assertEqual(value[0], chr(3))

    def test_every_code_mirc_uses_decodes(self):
        for escape, expected in (("\\x02", chr(2)),    # bold
                                 ("\\x1F", chr(31)),   # underline
                                 ("\\x0F", chr(15)),   # reset
                                 ("\\x03", chr(3))):   # colour
            with self.subTest(escape=escape):
                self.assertEqual(self.coerce(escape), expected)

    def test_lowercase_hex_works_too(self):
        self.assertEqual(self.coerce("\\x0f"), chr(15))

    def test_a_control_character_pasted_directly_is_left_alone(self):
        """An operator who managed to paste the real byte keeps it."""
        self.assertEqual(self.coerce(chr(3) + "04"), chr(3) + "04")

    def test_text_with_no_escapes_is_unchanged(self):
        self.assertEqual(self.coerce("plain"), "plain")

    def test_other_backslashes_are_not_touched(self):
        r"""Only \xHH, deliberately: a decoder that ate every backslash would
        quietly mangle a Windows path in some future string setting."""
        self.assertEqual(settings_file.decode_irc_escapes("D:\\MEDIA\\New"),
                         "D:\\MEDIA\\New")

    def test_only_theme_settings_are_decoded(self):
        """The narrowness is the point. A password or a path must survive
        exactly as typed."""
        raw = "hunter\\x02two"

        self.assertEqual(settings_file.coerce("SOME_OTHER_SETTING", raw,
                                              None, str),
                         raw)


if __name__ == "__main__":
    unittest.main()
