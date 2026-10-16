"""#782: a label that fits by the table still touched its neighbour on a real mIRC.

#767 made every label fit by a Tahoma 8pt character table; the operator's
screenshot showed `Side panel with slots, queue and today's totals`, which the
table said had 10.8 % to spare, running to the edge of its control and up against
the box beside it. The real dialog font renders wider than the table, so fitting
is not enough: every label needs room to spare, and the columns need air between
them.

The dialog is the settings window now (#1288 made the Options dialog its
Script tab), and most of its labels are generated: the generator wraps a
label as if its control were a fifth narrower (build_settings_window.FONT_SLACK),
so a label on one line has that fifth to spare by construction. The
hand-placed ones, the widgets', are held to the same rule here.
"""

import re
import sys
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests import test_the_options_dialog_labels_fit_their_controls as fit  # noqa: E402

MIN_SLACK = 0.20          # room to spare, as a fraction of the control
MIN_COLUMN_GAP_DBU = 8    # between a check and the control beside it
LINE_DBU = 8              # a text taller than one line wraps

CONTROL = re.compile(r'^\s*(check|text|button)\s+"([^"]*)",\s*(\d+),\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', re.M)


def controls():
    for match in CONTROL.finditer(fit.dialog_table()):
        kind, label = match.group(1), match.group(2)
        ident, x, y, w, h = (int(v) for v in match.groups()[2:])
        yield kind, label, ident, x, y, w, h


def need_px(label):
    return sum(fit.CHAR_PX.get(ch, 6) for ch in label)


def room_px(kind, width):
    return width * fit.PX_PER_DBU - (fit.CHECK_BOX_PX if kind == "check" else 0)


class EveryLabelHasRoomToSpare(unittest.TestCase):

    def test_at_least_a_fifth_is_left_over(self):
        """Every label on one line: a text given more lines wraps, and
        the generator's own measure decides where."""
        tight = []
        for kind, label, ident, x, y, w, h in controls():
            if not label or (kind == "text" and h > LINE_DBU):
                continue
            room = room_px(kind, w)
            slack = (room - need_px(label)) / room
            if slack < MIN_SLACK:
                tight.append(f"{ident} {label!r}: {slack * 100:.1f}%")
        self.assertEqual(tight, [], "labels with less than 20% room to spare")

    def test_the_generator_wraps_with_that_fifth_to_spare(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "mirc"))
        import build_settings_window as generator
        self.assertLessEqual(generator.FONT_SLACK, 1 - MIN_SLACK)
        # a label that fills 85% of its width by the table is given two lines
        label = "x" * int(100 * fit.PX_PER_DBU * 0.85 / fit.CHAR_PX["x"])
        self.assertEqual(generator.wrapped_lines(label.replace("xx", "x ", 1), 100), 2)
        self.assertFalse(generator.Builder.check_fits(None, "y" * int(100 * fit.PX_PER_DBU * 0.85 / fit.CHAR_PX["y"]),
                                                      100))

    def test_the_users_own_label_is_no_longer_the_tightest_cut(self):
        panel = next((kind, label, w) for kind, label, ident, x, y, w, h in controls()
                     if label.startswith("Side panel"))
        kind, label, width = panel
        self.assertNotIn("today's totals", label)
        self.assertGreater((room_px(kind, width) - need_px(label)) / room_px(kind, width), 0.30)

    def test_the_controls_were_found(self):
        self.assertGreater(len(list(controls())), 200)


class TheColumnsHaveAirBetweenThem(unittest.TestCase):

    def rows(self):
        table = fit.dialog_table()
        for match in re.finditer(r'^\s*(check|combo|text|edit)\s+(?:"[^"]*",\s*)?(\d+),\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)',
                                 table, re.M):
            kind = match.group(1)
            ident, x, y, w, h = (int(v) for v in match.groups()[1:])
            yield kind, ident, x, y, w, h

    def test_a_check_and_the_control_to_its_right_on_the_same_row_are_apart(self):
        """The Script tab's line types (a check, its colour menu beside it)
        and the font check with its size: the pairs the Options dialog had."""
        by_id = {r[1]: r for r in self.rows()}
        pairs = [(1620 + 2 * r, 1621 + 2 * r) for r in range(8)] + [(1670, 1671)]
        for check_id, beside_id in pairs:
            _k, ident, x, y, w, h = by_id[check_id]
            _k2, ident2, x2, y2, w2, h2 = by_id[beside_id]
            self.assertLess(abs(y - y2), h, f"{ident} and {ident2} are not on one row")
            self.assertGreaterEqual(x2 - (x + w), MIN_COLUMN_GAP_DBU, f"control {ident} runs too close to {ident2}")

    def test_nothing_runs_past_the_right_edge_of_the_dialog(self):
        width = int(re.search(r"size -1 -1 (\d+) \d+", fit.dialog_table()).group(1))
        for kind, ident, x, y, w, h in self.rows():
            self.assertLessEqual(x + w, width - 4, f"control {ident} runs to the dialog's edge")


if __name__ == "__main__":
    unittest.main()
