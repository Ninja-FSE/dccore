"""#782: a label that fits by the table still touched its neighbour on a real mIRC.

#767 made every label fit by a Tahoma 8pt character table; the operator's
screenshot showed `Side panel with slots, queue and today's totals`, which the
table said had 10.8 % to spare, running to the edge of its control and up against
the box beside it. The real dialog font renders wider than the table, so fitting
is not enough: every label needs room to spare, and the columns need air between
them.
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
MIN_COLUMN_GAP_DBU = 8    # between the left column's controls and the right's

CONTROL = re.compile(r'^\s*(check|text)\s+"([^"]*)",\s*(\d+),\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', re.M)


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
        tight = []
        for kind, label, ident, x, y, w, h in controls():
            if not label:
                continue
            room = room_px(kind, w)
            slack = (room - need_px(label)) / room
            if slack < MIN_SLACK:
                tight.append(f"{ident} {label!r}: {slack * 100:.1f}%")
        self.assertEqual(tight, [], "labels with less than 20% room to spare")

    def test_the_users_own_label_is_no_longer_the_tightest_cut(self):
        by_id = {ident: (kind, label, w) for kind, label, ident, x, y, w, h in controls()}
        kind, label, width = by_id[301]
        self.assertNotIn("today's totals", label)
        self.assertGreater((room_px(kind, width) - need_px(label)) / room_px(kind, width), 0.30)

    def test_the_controls_were_found(self):
        self.assertGreater(len(list(controls())), 15)


class TheColumnsHaveAirBetweenThem(unittest.TestCase):

    def rows(self):
        table = fit.dialog_table()
        for match in re.finditer(r'^\s*(check|combo|text|edit)\s+(?:"[^"]*",\s*)?(\d+),\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', table, re.M):
            kind = match.group(1)
            ident, x, y, w, h = (int(v) for v in match.groups()[1:])
            yield kind, ident, x, y, w, h

    def test_a_check_and_the_control_to_its_right_on_the_same_row_are_apart(self):
        rows = list(self.rows())
        checks = [r for r in rows if r[0] == "check" and r[1] in (101, 102, 103, 104, 105, 106, 107, 108, 301, 302, 303)]
        others = [r for r in rows if r[1] in (201, 202, 203, 204, 205, 206, 207, 208, 304)]
        for _k, ident, x, y, w, h in checks:
            for _k2, ident2, x2, y2, w2, h2 in others:
                if abs(y - y2) < h and x2 > x:
                    self.assertGreaterEqual(x2 - (x + w), MIN_COLUMN_GAP_DBU,
                                            f"control {ident} runs too close to {ident2}")

    def test_nothing_runs_past_the_right_edge_of_its_box(self):
        for kind, ident, x, y, w, h in self.rows():
            self.assertLessEqual(x + w, 317, f"control {ident} leaves the group box")


if __name__ == "__main__":
    unittest.main()
