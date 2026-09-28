"""The @DCCore window's side panel keeps the button's colour, and is not
repeated in the text (#1013).

Seen live: the @DCCore button turned red for a new line and went black again
before anyone looked. Every status burst - about every 30 seconds - redraws
the side panel, and the redraw starts with `clear -l`, which resets the
window button's colour as well; checked in mIRC: `/echo -m @DCCore test`
lights it, `/clear -l @DCCore` puts it out. And with the panel on, the text
filled with identical [STATUS] lines saying what the panel already showed.

mIRC cannot run here, so this reads the script's code lines, comments
stripped, so a comment naming a switch cannot satisfy it.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

# Imported as a module, not by name, so no TestCase is collected twice.
import tests.test_the_console_window_lights_up_like_a_channel as lights  # noqa: E402


class TheRedrawPutsTheColourBack(unittest.TestCase):

    def panel(self):
        return lights.alias_body("dccore.panel")

    def test_the_colour_is_read_before_the_clear(self):
        body = self.panel()
        self.assertLess(body.index("var %lit = $dccore.lit"), body.index("clear -l $dccore.win"))

    def test_and_set_again_on_every_way_out(self):
        body = self.panel()
        self.assertEqual(body[-1], "dccore.relight %lit", "after the last line is drawn")
        after_clear = body[body.index("clear -l $dccore.win"):]
        exits = [n for n, line in enumerate(after_clear) if "return" in line]
        self.assertEqual(len(exits), 1, "one early way out once the panel is cleared: plain mode")
        self.assertEqual(after_clear[exits[0] - 1], "dccore.relight %lit", "and it puts the colour back too")

    def test_the_two_colours_that_matter_and_nothing_else(self):
        lit = lights.alias_body("dccore.lit")
        self.assertIn("if ((%c == 2) || (hi isin %c)) { return 2 }", lit)
        self.assertIn("if ((%c == 1) || (mess isin %c)) { return 1 }", lit)
        self.assertEqual(lit[-1], "return 0")
        self.assertIn("($active == $dccore.win)", lit[0], "the active window's button is never coloured")

    def test_set_with_window_g_and_not_on_the_active_window(self):
        (line,) = lights.alias_body("dccore.relight")
        self.assertIn("($1 isnum 1-2)", line)
        self.assertIn("($active != $dccore.win)", line)
        self.assertIn("window -g $+ $1 $dccore.win", line)


class NoStatusLineBesideThePanel(unittest.TestCase):

    def status_echo_condition(self):
        body = lights.alias_body("dccore.status")
        (line,) = [line for line in body if "dccore.opt(statusmin)" in line]
        return line

    def test_the_line_is_written_only_without_the_panel(self):
        self.assertIn("(!$dccore.opt(panel))", self.status_echo_condition())

    def test_it_still_follows_the_interval_setting(self):
        condition = self.status_echo_condition()
        self.assertIn("($dccore.opt(statusmin) > 0)", condition)
        self.assertIn("$calc($dccore.opt(statusmin) * 60)", condition)

    def test_the_panel_is_still_drawn_from_every_burst(self):
        self.assertIn("dccore.panel.soon", lights.alias_body("dccore.status"))


if __name__ == "__main__":
    unittest.main()
