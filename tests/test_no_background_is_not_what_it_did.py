"""A background you did not set is the one that was already there.

Reported from the beta, against the colour picker: *"something is wrong with
color preview in website"*, and then the sharper version - *"why half message
is shown in red and other half in blue"*.

NOTHING WAS WRONG WITH THE PREVIEW. It was showing exactly what the channel
would see. What was wrong was the menu that produced the setting.

The picker offered a background of **"No background"**, which is not what that
choice does. A colour code carrying only a foreground leaves the background as
the previous segment set it. That is the IRC formatting spec, not an
implementation quirk:

    "If only the foreground color is set, the background color stays the same."

So a text box set to a foreground alone does not clear its background - it
inherits whichever block ran before it, forever, because nothing later sets
one.

WHY THE MESSAGE SPLIT IN THREE. The advert opens

    {BORDER} {SEPARATOR} {TEXTBOX} Type: ...

and every later section is

    {SEPARATOR} {BORDER} {TEXTBOX} Slots: ...

- the two blocks in the opposite order. So the first field inherits the
SEPARATOR's colour and every later field inherits the BORDER's, which is the
blue half and the red half. The stretch between them is on the client default,
because a reset clears both and the text box never puts one back.

Three backgrounds, no bug, and no way for the operator to know from a menu
that said "No background".

THE FIX IS THE LABEL, AND A WARNING WHERE IT MATTERS. theme.py already divides
the roles: border is "the outer block that frames a section", separator "the
block between fields", textbox "the plate the text sits on" - a block with no
background is not a block. value, alert and accent colour figures and text,
where a foreground alone is exactly right and no warning belongs.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import theme  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BACKSLASH = chr(92)


def read(name):
    with io.open(os.path.join(REPO_ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


def code_only():
    return re.sub(r"//[^\n]*", "", read("app.js"))


def function(name):
    text = code_only()
    head = text.index("function %s(" % name)
    line_start = text.rfind("\n", 0, head) + 1
    indent = " " * (head - line_start)
    return text[head:].split("\n" + indent + "}", 1)[0]


def backgrounds_in(line):
    """Every visible run of text, paired with the background it lands on.

    A small mIRC reader: it exists because the claim under test is about what
    a CLIENT does with these codes, and asserting on the codes themselves
    would only restate the template.
    """
    out = []
    fg = bg = None
    run = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\x03":
            i += 1
            digits = ""
            while len(digits) < 2 and i < len(line) and line[i].isdigit():
                digits += line[i]
                i += 1
            if run.strip():
                out.append((bg, run.strip()))
            run = ""
            if digits:
                fg = int(digits)
                if i < len(line) and line[i] == ",":
                    after = ""
                    j = i + 1
                    while len(after) < 2 and j < len(line) and line[j].isdigit():
                        after += line[j]
                        j += 1
                    if after:
                        bg = int(after)
                        i = j
            else:
                fg = bg = None
            continue
        if ch == "\x0f":
            if run.strip():
                out.append((bg, run.strip()))
            run = ""
            fg = bg = None
            i += 1
            continue
        if ch == "\x02":
            i += 1
            continue
        run += ch
        i += 1
    if run.strip():
        out.append((bg, run.strip()))
    return out


class AForegroundOnlyCodeKeepsTheBackground(DCCoreTestCase):
    """The reported symptom, reproduced from the operator's own picks."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="DCCore", CHANNEL="#somechannel")

    def advert(self):
        over = webserver.theme_preview_overrides({
            "THEME": "midnight",
            "CUSTOM_THEME_BORDER": BACKSLASH + "x0302,04",   # blue on red
            "CUSTOM_THEME_TEXTBOX": BACKSLASH + "x0310",     # cyan, no bg
        })
        return webserver.build_theme_preview(over)["advert"]

    def test_the_text_box_carries_no_background_of_its_own(self):
        over = webserver.theme_preview_overrides(
            {"THEME": "midnight", "CUSTOM_THEME_TEXTBOX": BACKSLASH + "x0310"})

        self.assertNotIn(",", theme.palette(over)["textbox"])

    def test_the_message_lands_on_more_than_one_background(self):
        """The report, as an assertion. One plate is the point of a text box;
        this configuration produces three."""
        seen = {bg for bg, _text in backgrounds_in(self.advert())}

        self.assertGreater(len(seen), 1,
                           "the split that was reported did not happen, so "
                           "this test is no longer about the reported bug")

    def test_the_first_field_inherits_the_separator_and_the_rest_the_border(self):
        """Not an accident of the theme: the advert opens BORDER SEPARATOR
        TEXTBOX and every later section is SEPARATOR BORDER TEXTBOX, so the
        block immediately before the text differs."""
        landed = backgrounds_in(self.advert())
        by_text = {text: bg for bg, text in landed}

        self.assertEqual(by_text["Type:"], 12, "expected the separator's colour")
        self.assertEqual(by_text["Slots: 3/3"], 4, "expected the border's colour")

    def test_a_reset_drops_to_the_client_default_and_nothing_restores_it(self):
        """The third background. \\x0f clears both, and a foreground-only text
        box never puts one back."""
        landed = backgrounds_in(self.advert())
        by_text = {text: bg for bg, text in landed}

        self.assertIsNone(by_text["Files (5.48 TB) created"])

    def test_a_text_box_WITH_a_background_puts_every_field_on_one_plate(self):
        """The same configuration, corrected - which is what the warning
        exists to steer the operator towards."""
        over = webserver.theme_preview_overrides({
            "THEME": "midnight",
            "CUSTOM_THEME_BORDER": BACKSLASH + "x0302,04",
            "CUSTOM_THEME_TEXTBOX": BACKSLASH + "x0310,01",   # cyan on black
        })
        landed = backgrounds_in(webserver.build_theme_preview(over)["advert"])
        by_text = {text: bg for bg, text in landed}

        self.assertEqual(by_text["Type:"], 1)
        self.assertEqual(by_text["Slots: 3/3"], 1)

    def test_every_shipped_preset_gives_all_three_surfaces_a_background(self):
        """Which is why no preset has ever shown this, and why the picker
        letting one through is the defect."""
        for name, preset in theme.THEMES.items():
            if name == "plain":
                continue      # deliberately no codes at all
            for role in ("border", "separator", "textbox"):
                with self.subTest(theme=name, role=role):
                    self.assertIn(",", preset[role],
                                  "%s's %s is drawn as a block or a plate and "
                                  "needs a background" % (name, role))


class TheMenuNoLongerClaimsThereIsNoBackground(unittest.TestCase):

    def test_the_option_says_what_it_does(self):
        picker = function("ircColourPickerHtml")

        self.assertIn('options(picked.bg, "Keep previous")', picker)

    def test_and_no_longer_says_what_it_does_not(self):
        self.assertNotIn('"No background"', code_only())


class TheWarningIsOnlyWhereItBelongs(unittest.TestCase):

    def warning(self):
        return function("surfaceWarningHtml")

    def test_the_three_surfaces_are_named_from_what_they_are(self):
        source = code_only()
        listed = source.split("var IRC_SURFACE_ROLES = [", 1)[1].split("]", 1)[0]

        self.assertIn("BORDER", listed)
        self.assertIn("SEPARATOR", listed)
        self.assertIn("TEXTBOX", listed)

    def test_and_the_text_colours_are_not(self):
        """value, alert and accent are foregrounds by design. Warning on them
        would be telling the operator off for using the feature correctly."""
        source = code_only()
        listed = source.split("var IRC_SURFACE_ROLES = [", 1)[1].split("]", 1)[0]

        for role in ("VALUE", "ALERT", "ACCENT"):
            with self.subTest(role=role):
                self.assertNotIn(role, listed)

    def test_the_guard_is_the_role_check_itself(self):
        """Asserting the pieces below is not enough: replacing this line with
        `if (true)` leaves every one of them present as dead code, and a
        mutation run said so. The guard has to be named."""
        self.assertIn('if (!isSurfaceRole(settingName)) { return ""; }',
                      self.warning())

    def test_nothing_is_said_when_a_background_is_set(self):
        self.assertIn('picked.bg !== ""', self.warning())

    def test_nor_when_the_role_is_left_at_the_theme_default(self):
        """No foreground means no override at all - the preset's own value is
        used, and every preset gives these three a background."""
        self.assertIn('picked.fg === ""', self.warning())

    def test_it_says_which_way_round_the_problem_is(self):
        body = self.warning()

        self.assertIn("keeps the previous background", body)
        self.assertIn("drawn as a block", body)

    def test_it_is_visible_rather_than_decorative(self):
        css = read("style.css")
        rule = css.split(".irc-colour-warning {", 1)[1].split("}", 1)[0]

        self.assertIn("var(--danger)", rule)


if __name__ == "__main__":
    unittest.main()
