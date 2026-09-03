"""The dashboard's two themes.

Every colour on the page already came from a token, so a second theme is the
same names with different values. That is also what makes it fragile in one
specific way: the moment a colour is written directly into a component rule, it
stops following the theme, and it does so silently - the page still renders,
one thing in it is just the wrong colour on one of the two backgrounds.

So the first test here is not about the light theme at all. It is that no
colour is written outside the palette.

THE VIEWER HAS THREE STATES, NOT TWO

Somebody who has never touched the toggle has chosen nothing, and the root
element carries no data-theme - that state is decided by prefers-color-scheme
alone. An explicit choice must then beat the operating system in BOTH
directions, which is why there are two light blocks rather than one.
"""

import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO_ROOT, "web")


def is_colour(value):
    """Whether a token's value is a colour, from the value itself.

    Every palette entry is a hex literal or an rgba()/rgb()/hsl() call; a
    layout token is a length, a calc() or a font stack. Asking the value
    means a new non-colour token needs no edit here at all.
    """
    text = str(value).strip().lower()
    return text.startswith("#") or text.startswith(("rgb(", "rgba(", "hsl(", "hsla("))


def read(name):
    with io.open(os.path.join(WEB, name), encoding="utf-8") as handle:
        return handle.read()


def strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def block_after(css, selector):
    """The body of the first rule whose selector contains `selector`."""
    at = css.index(selector)
    start = css.index("{", at) + 1
    depth, index = 1, start
    while depth:
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
        index += 1
    return css[start:index - 1]


def tokens_in(body):
    return dict(re.findall(r"(--[a-z-]+)\s*:\s*([^;]+);", body))


def top_level_rules(css):
    """[(selector, body)] for every rule at the top level of the stylesheet.

    A rule inside @media comes back as part of that @media block rather than on
    its own - enough to tell a palette block from a component rule, which is
    all this file needs.
    """
    rules, depth, start, body_start = [], 0, 0, 0
    selector_start = 0
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                selector_start = start
                body_start = index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                rules.append((css[selector_start:body_start - 1].strip(),
                              css[body_start:index]))
                start = index + 1
    return rules


def palette_and_components(css):
    """(palette rules, component rules).

    A palette rule defines tokens on the root element. An @media block wrapping
    one counts as palette too, which is why the whole block is inspected rather
    than its selector alone.
    """
    palette, components = [], []
    for selector, body in top_level_rules(css):
        if ":root" in selector or (selector.startswith("@media") and ":root" in body):
            palette.append((selector, body))
        else:
            components.append((selector, body))
    return palette, components


def luminance(colour):
    """WCAG relative luminance for a #rrggbb string."""
    digits = colour.lstrip("#")
    channels = [int(digits[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(one, two):
    first, second = luminance(one), luminance(two)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


class EveryColourComesFromThePalette(unittest.TestCase):
    """The property the whole feature rests on. A colour written into a
    component rule follows neither theme, and nothing about the page breaks to
    say so - it is simply wrong on one of the two backgrounds."""

    def test_no_colour_is_written_outside_the_palette_blocks(self):
        """Block by block rather than by text offset. An earlier version of
        this cut the file at a fixed marker and, once the theme blocks were
        inserted above that marker, scanned nothing at all - it passed while
        the rules it was written to police sat outside the region it looked
        at. The mutation run is what said so."""
        _palette, components = palette_and_components(strip_comments(read("style.css")))

        offenders = []
        for selector, body in components:
            for line in body.split("\n"):
                if re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", line):
                    offenders.append("%s { %s }" % (selector, line.strip()))

        self.assertEqual(
            offenders, [],
            "colour written directly into a rule instead of coming from a "
            "token - it will not follow the theme: " + "; ".join(offenders))

    def test_the_scan_actually_reaches_the_component_rules(self):
        """The control the earlier version lacked. If the split ever puts
        everything on the palette side again, the test above would pass on a
        stylesheet full of hard-coded colours."""
        palette, components = palette_and_components(strip_comments(read("style.css")))

        self.assertGreater(len(components), 50,
                           "almost nothing was classed as a component rule")
        self.assertIn(".theme-btn", " ".join(s for s, _b in components),
                      "the theme switch's own rules are not being scanned")
        self.assertTrue(palette, "no palette block was recognised")

    def test_the_script_and_the_page_set_no_colours_either(self):
        """A colour assigned from JavaScript is the same bug somewhere the
        stylesheet tests cannot see it."""
        for name in ("app.js", "index.html"):
            with self.subTest(file=name):
                source = re.sub(r"//[^\n]*", "", read(name))
                found = re.findall(r"#[0-9a-fA-F]{6}\b|rgba?\([\d\s,.]+\)", source)

                self.assertEqual(found, [], "%s sets a colour of its own" % name)


class BothThemesDefineTheSameNames(unittest.TestCase):
    """A token the light theme forgets keeps its dark value, so one element
    stays dark-on-light. Nothing errors; it just looks broken."""

    def setUp(self):
        self.css = strip_comments(read("style.css"))
        self.dark = tokens_in(block_after(self.css, ":root {"))
        self.light = tokens_in(block_after(self.css, ':root[data-theme="light"]'))
        self.system = tokens_in(block_after(self.css, ':root:not([data-theme="dark"])'))

    def test_the_dark_palette_is_the_base(self):
        self.assertGreaterEqual(len(self.dark), 15,
                                "the palette shrank - are these still tokens?")

    def test_the_light_theme_redefines_every_colour(self):
        """Which tokens are colours is decided by their VALUE, not by a list
        of name prefixes.

        It was a prefix list - "--radius" and "--font" - and that is the same
        shape as every other check in this repo built from a list: it finds
        what is on the list. The first layout token added to the palette block
        that did not happen to start with one of those two names failed this
        test for being the wrong KIND of thing rather than for being wrong,
        and the fix would have been to lengthen the list again.
        """
        colours = {name for name, value in self.dark.items() if is_colour(value)}

        self.assertEqual(colours - set(self.light), set(),
                         "token(s) with no light value, so they stay dark on a "
                         "light page")

    def test_the_colour_test_actually_recognises_the_palette(self):
        """Control. A classifier that called nothing a colour would let the
        test above pass on a palette with no light theme at all."""
        colours = {name for name, value in self.dark.items() if is_colour(value)}

        self.assertGreaterEqual(len(colours), 15, sorted(self.dark))
        self.assertNotIn("--radius", colours)
        self.assertNotIn("--font-sans", colours)

    def test_and_it_recognises_each_form_the_palette_actually_uses(self):
        for value in ("#0b0f14", "#fff", "rgba(45, 212, 200, 0.12)", "rgb(1,2,3)",
                      "hsl(200 10% 5%)"):
            with self.subTest(value=value):
                self.assertTrue(is_colour(value))

        for value in ("10px", "calc(100vh - 300px)", "67px", '"IBM Plex Sans", sans-serif'):
            with self.subTest(value=value):
                self.assertFalse(is_colour(value))

    def test_the_two_light_blocks_agree(self):
        """One is for an operating system set to light, the other for an
        explicit choice. They are the same theme, so a value changed in one and
        not the other means the toggle and the OS disagree about what "light"
        looks like."""
        self.assertEqual(self.system, self.light)

    def test_the_light_theme_adds_nothing_the_dark_one_lacks(self):
        """A token only the light theme defines is undefined in dark, which
        renders as nothing at all rather than as a colour."""
        self.assertEqual(set(self.light) - set(self.dark), set())


class AnExplicitChoiceBeatsTheOperatingSystem(unittest.TestCase):
    """Three states: no attribute (the OS decides), data-theme="dark", and
    data-theme="light". The last two have to win in both directions."""

    def setUp(self):
        self.css = strip_comments(read("style.css"))

    def test_the_system_block_is_guarded_against_an_explicit_dark(self):
        """Without :not([data-theme="dark"]), somebody who picks dark on a
        machine set to light gets light anyway, and the toggle looks broken in
        exactly one direction."""
        self.assertIn("prefers-color-scheme: light", self.css)
        guard = self.css.index("prefers-color-scheme: light")

        self.assertIn(':root:not([data-theme="dark"])', self.css[guard:guard + 200])

    def test_an_explicit_light_wins_on_a_dark_machine(self):
        """The second block. Without it the media query never matches and the
        toggle does nothing for anyone whose OS is set to dark - which is most
        of the people who would want this."""
        self.assertIn(':root[data-theme="light"]', self.css)


class TheLightGroundIsGreyAndReadable(unittest.TestCase):
    """The numbers behind the palette, so "it looked fine" is not the standard.

    Measured against the CARD colour rather than the page, because that is the
    surface most text actually sits on.
    """

    def setUp(self):
        css = strip_comments(read("style.css"))
        self.light = tokens_in(block_after(css, ':root[data-theme="light"]'))
        self.dark = tokens_in(block_after(css, ":root {"))
        self.card = self.light["--bg-raised"].strip()
        self.page = self.light["--bg"].strip()

    def test_the_page_is_grey_rather_than_white(self):
        """Asked for explicitly, and it matters beyond taste: cards are the
        lighter surface here, so a white page would leave them with nothing to
        be raised against."""
        self.assertNotEqual(self.page.lower(), "#ffffff")
        self.assertGreater(
            contrast(self.page, "#ffffff"), 1.15,
            "the page ground is so close to white that the cards on it "
            "disappear")

    def test_cards_are_lighter_than_the_page_they_sit_on(self):
        self.assertGreater(luminance(self.card), luminance(self.page))

    def test_body_text_is_comfortably_readable(self):
        self.assertGreaterEqual(contrast(self.light["--text"].strip(), self.card), 7.0)

    def test_secondary_text_meets_aa(self):
        self.assertGreaterEqual(contrast(self.light["--text-dim"].strip(), self.card), 4.5)

    def test_every_semantic_colour_meets_aa_on_a_card(self):
        """The accent and the status colours are read as text, not just seen as
        decoration. The dark theme's teal sits at about 1.5 to 1 on a light
        ground, which is why none of these could simply be reused."""
        for token in ("--accent", "--status-frozen", "--status-queued", "--danger"):
            with self.subTest(token=token):
                self.assertGreaterEqual(
                    contrast(self.light[token].strip(), self.card), 4.5)

    def test_button_text_is_readable_on_the_accent_fill(self):
        """--on-accent was the last colour literal in the file. It has to flip
        with the theme: near-black on the bright dark-theme teal, white on the
        darkened light-theme one."""
        for palette in (self.dark, self.light):
            with self.subTest(accent=palette["--accent"].strip()):
                self.assertGreaterEqual(
                    contrast(palette["--on-accent"].strip(),
                             palette["--accent"].strip()), 4.5)

    def test_the_dark_theme_was_not_made_worse(self):
        """A regression guard on the theme that already existed and that people
        are using today."""
        card = self.dark["--bg-raised"].strip()
        self.assertGreaterEqual(contrast(self.dark["--text"].strip(), card), 7.0)
        self.assertGreaterEqual(contrast(self.dark["--text-dim"].strip(), card), 4.5)


class TheChoiceIsAppliedBeforeTheFirstPaint(unittest.TestCase):
    """A stored choice applied from app.js renders the page in the OS theme
    first and swaps - a flash on every load for anyone whose choice differs
    from their machine's."""

    def setUp(self):
        self.html = read("index.html")
        self.head = self.html[:self.html.index("</head>")]

    def test_the_head_reads_the_stored_choice_and_applies_it(self):
        script = self.head[self.head.index("<script>"):]

        self.assertIn('localStorage.getItem("dccore-theme")', script,
                      "the head script does not read the stored choice")
        self.assertIn('setAttribute("data-theme"', script,
                      "the head script reads the choice and never applies it")

    def test_reading_the_stored_choice_cannot_throw(self):
        """A browser set to block site data throws on localStorage access
        rather than returning null, and an uncaught throw in the head stops
        every script after it - which here is the whole dashboard."""
        script = self.head[self.head.index("<script>"):]

        self.assertIn("try", script)
        self.assertIn("catch", script)

    def test_no_stored_choice_leaves_the_attribute_off(self):
        """Which is what hands the decision to prefers-color-scheme. Writing a
        default in would override the operating system for everyone who has
        never expressed an opinion."""
        self.assertNotIn('setAttribute("data-theme", "dark")', self.head)


class TheSwitchIsWired(unittest.TestCase):
    """A control the page shows and the script never acts on does nothing, and
    looks like it should."""

    def setUp(self):
        self.html = read("index.html")
        self.js = read("app.js")

    def test_both_buttons_exist_on_the_page(self):
        for element_id in ("theme-dark", "theme-light"):
            with self.subTest(id=element_id):
                self.assertIn('id="%s"' % element_id, self.html)

    def test_the_script_reads_them(self):
        self.assertIn('getElementById("theme-dark")', self.js)
        self.assertIn('getElementById("theme-light")', self.js)

    def test_clicking_a_button_changes_the_theme(self):
        """A listener that reads the button and does nothing with it is the
        same as no listener, and looks identical in the markup."""
        # Scoped to the theme section: app.js wires several click handlers and
        # the nav's is the first in the file, so an unanchored search checks
        # the wrong one and reports on it confidently.
        section = self.js[self.js.index("THEME_KEY"):]
        handler = section[section.index('addEventListener("click"'):]
        handler = handler[:handler.index("});")]

        self.assertIn("chooseTheme(", handler)
        self.assertIn("data-theme-choice", handler)

    def test_choosing_applies_the_theme_and_remembers_it(self):
        body = self.js[self.js.index("function chooseTheme"):]
        body = body[:body.index("markThemeButtons();")]

        self.assertIn('setAttribute("data-theme"', body)
        self.assertIn("localStorage.setItem(THEME_KEY", body)

    def test_every_use_of_local_storage_is_guarded(self):
        """Same throw as the head script, in the place that would take the
        click handler down with it."""
        for match in re.finditer(r"localStorage\.(get|set)Item", self.js):
            with self.subTest(at=match.start()):
                before = self.js[max(0, match.start() - 400):match.start()]

                self.assertIn("try {", before)

    def test_the_choice_is_remembered_per_browser_not_sent_to_the_daemon(self):
        """Which theme suits a screen is a fact about the person looking at it.
        The same daemon gets looked at from a phone in a dark room and a
        desktop by a window, so this is not a setting the bot should hold."""
        section = self.js[self.js.index("THEME_KEY"):]
        section = section[:section.index("markThemeButtons();")]

        self.assertNotIn("fetch(", section)
        self.assertNotIn("/api/", section)


if __name__ == "__main__":
    unittest.main()
