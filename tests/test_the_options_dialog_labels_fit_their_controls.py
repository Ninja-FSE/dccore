"""#616: the labels in /dccore options were wider than their controls.

A mIRC dialog with `option dbu` measures in dialog base units: one
horizontal unit is a quarter of the dialog font's average character width,
and Windows derives that width from the 52-letter alphabet. mIRC draws its
dialogs in MS Shell Dlg, which is Tahoma 8pt; at 96 DPI the base unit is
6px, so 1 dbu = 1.5px. A check control also spends about 17px on the box
itself. A label that needs more than that is not wrapped - the end is cut
off, and "Side panel with slots, queue and today's totals" read as
"Side panel with slots, queue and" in a 118 dbu control.

mIRC cannot be run here, so the dialog table is measured from the source
the way the audit did: every label's pixel width against its control. Two
tests do the arithmetic. TheLabelsFit uses a per-character width table of
Tahoma 8pt at 96 DPI taken from GDI once (GetTextExtentPoint32 does not
kern, so the sum of the characters IS the string width), and runs on any
platform. TheLabelsFitInGdi asks the real font on a Windows box and skips
elsewhere, so a table that has drifted from the font is caught where the
font is.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")

# Tahoma 8pt at 96 DPI (the dialog font of a mIRC dialog), pixels per
# character, grouped by width. Taken from GDI on Windows 11.
_BY_WIDTH = {
    2: "'il",
    3: " j",
    4: "!\"(),-./:;I[\\]frt|",
    5: "?JLcksz{}",
    6: "$*0123456789BEFKPSTVXYZ_`abdeghnopquvxy",
    7: "&ACDGHNRU",
    8: "#+<=>MOQ^mw~",
    10: "@W",
    11: "%",
}
CHAR_PX = {c: w for w, chars in _BY_WIDTH.items() for c in chars}

PX_PER_DBU = 1.5      # dialog base unit 6px / 4
CHECK_BOX_PX = 17     # the square and the gap before the label

# What the init handler writes into the empty text 403 (both branches of
# its $iif), with a sample date where $dccore.opt(paired) goes.
PAIRED_STATUS = "Paired 20/09/2026 (token in dccore.ini)"
NOT_PAIRED_STATUS = "Not paired: the bot will ask for the password"


def script():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def dialog_table():
    source = script()
    table = source[source.index("dialog dccore.opt {"):]
    return table[:table.index("\n}\n")]


CONTROL = re.compile(
    r'^\s+(check|text|button|box|edit|combo) (?:"([^"]*)", )?(\d+), (\d+) (\d+) (\d+) (\d+)',
    re.M)


def controls():
    """(kind, label, id, x, y, w, h) for every control in the dialog."""
    found = [(k, label or "", int(i), int(x), int(y), int(w), int(h))
             for k, label, i, x, y, w, h in CONTROL.findall(dialog_table())]
    assert len(found) > 40, "the dialog scan found almost nothing"
    return found


def labelled():
    """Every (id, label, available px) pair that has a label to fit."""
    rows = []
    for kind, label, cid, _x, _y, w, _h in controls():
        if kind not in ("check", "text", "button"):
            continue
        texts = [label]
        if cid == 403:
            texts = [PAIRED_STATUS, NOT_PAIRED_STATUS]
        available = w * PX_PER_DBU - (CHECK_BOX_PX if kind == "check" else 0)
        for text in texts:
            rows.append((cid, text, available))
    return rows


def table_width(text):
    return sum(CHAR_PX[c] for c in text)


class TheLabelsFit(unittest.TestCase):

    def test_the_status_texts_are_the_ones_the_init_handler_writes(self):
        source = script()
        self.assertIn("Paired $dccore.opt(paired) (token in dccore.ini)", source)
        self.assertIn(NOT_PAIRED_STATUS, source)
        self.assertIn("dccore.set paired $date", source)

    def test_every_label_is_in_the_width_table(self):
        for _cid, text, _available in labelled():
            missing = sorted(set(text) - set(CHAR_PX))
            self.assertEqual(missing, [], text)

    def test_every_label_fits_its_control(self):
        rows = labelled()
        self.assertGreater(len(rows), 25)
        clipped = ["%d %r needs %dpx, has %.1f" % (cid, text, table_width(text), available)
                   for cid, text, available in rows if table_width(text) > available]
        self.assertEqual(clipped, [])

    def test_the_table_would_have_caught_the_old_dialog(self):
        """Mutation check on the fixture: the 118 dbu check the audit named
        is clipped by this arithmetic, so a passing table means something."""
        available = 118 * PX_PER_DBU - CHECK_BOX_PX
        self.assertGreater(table_width("Side panel with slots, queue and today's totals"), available)
        self.assertGreater(table_width("Fixed-width font (Lucida Console)"), 100 * PX_PER_DBU - CHECK_BOX_PX)


@unittest.skipUnless(sys.platform == "win32" and hasattr(__import__("ctypes"), "windll"),
                     "measures with GDI: Windows only")
class TheLabelsFitInGdi(unittest.TestCase):
    """The same check against the real dialog font, where there is one."""

    FONTS = ("MS Shell Dlg", "Tahoma")

    def measure(self, font_name, texts):
        import ctypes

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

        gdi, user = ctypes.windll.gdi32, ctypes.windll.user32
        hdc = user.GetDC(0)
        # 8pt at 96 DPI is 11px tall; character set 1 is DEFAULT_CHARSET
        font = gdi.CreateFontW(-11, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0, font_name)
        old = gdi.SelectObject(hdc, font)
        size = SIZE()
        try:
            alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            gdi.GetTextExtentPoint32W(hdc, alphabet, 52, ctypes.byref(size))
            base_unit = (size.cx // 26 + 1) // 2
            widths = {}
            for text in texts:
                gdi.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(size))
                widths[text] = size.cx
        finally:
            gdi.SelectObject(hdc, old)
            gdi.DeleteObject(font)
            user.ReleaseDC(0, hdc)
        return base_unit, widths

    def test_the_base_unit_is_the_one_the_table_assumes(self):
        base_unit, _ = self.measure("Tahoma", [])
        self.assertEqual(base_unit / 4.0, PX_PER_DBU)

    def test_the_width_table_matches_the_font(self):
        texts = [text for _cid, text, _available in labelled()]
        _base, widths = self.measure("Tahoma", texts)
        for text in texts:
            self.assertEqual(widths[text], table_width(text), text)

    def test_every_label_fits_its_control_in_the_dialog_font(self):
        rows = labelled()
        for font_name in self.FONTS:
            base_unit, widths = self.measure(font_name, [text for _c, text, _a in rows])
            px_per_dbu = base_unit / 4.0
            clipped = []
            for cid, text, _table_available in rows:
                kind_is_check = any(k == "check" and i == cid for k, _l, i, *_ in controls())
                width = next(w for _k, _l, i, _x, _y, w, _h in controls() if i == cid)
                available = width * px_per_dbu - (CHECK_BOX_PX if kind_is_check else 0)
                if widths[text] > available:
                    clipped.append("%s: %d %r needs %dpx, has %.1f"
                                   % (font_name, cid, text, widths[text], available))
            self.assertEqual(clipped, [])


class TheControlsStayInTheirBoxes(unittest.TestCase):
    """Widening the dialog moved the whole right column; nothing may have
    been left hanging out of its group box or the dialog."""

    def test_every_control_is_inside_the_dialog(self):
        width, height = map(int, re.search(r"size -1 -1 (\d+) (\d+)", dialog_table()).groups())
        for kind, _label, cid, x, y, w, h in controls():
            if kind == "combo":
                h = 12   # the closed height; the fourth number is the dropped list
            self.assertLessEqual(x + w, width, cid)
            self.assertLessEqual(y + h, height, cid)

    def test_every_control_in_a_box_is_inside_it_horizontally(self):
        boxes = [(x, y, w, h) for kind, _l, _i, x, y, w, h in controls() if kind == "box"]
        # Show, Window, Connection - and DCCore Chat since #371.
        self.assertEqual(len(boxes), 4)
        checked = 0
        for kind, _label, cid, x, y, w, _h in controls():
            if kind == "box":
                continue
            for bx, by, bw, bh in boxes:
                if by <= y <= by + bh:
                    self.assertTrue(bx <= x and x + w <= bx + bw,
                                    "%d runs out of the box at %d..%d" % (cid, bx, bx + bw))
                    checked += 1
        self.assertGreater(checked, 35)

    def test_the_right_column_starts_after_the_left_checks_end(self):
        left = [(x + w) for kind, _l, i, x, _y, w, _h in controls()
                if kind == "check" and x == 10 and i < 400]
        right = [x for kind, _l, i, x, _y, _w, _h in controls()
                 if kind in ("combo", "check", "text") and 100 < i < 400 and x > 10]
        self.assertEqual(len(left), 11)
        self.assertLess(max(left), min(right))

    def test_the_size_edit_sits_after_its_label(self):
        font = next(c for c in controls() if c[2] == 305)
        edit = next(c for c in controls() if c[2] == 306)
        self.assertLessEqual(font[3] + font[5], edit[3])
        status = next(c for c in controls() if c[2] == 215)
        unit = next(c for c in controls() if c[2] == 216)
        self.assertLessEqual(status[3] + status[5], unit[3])


if __name__ == "__main__":
    unittest.main()
