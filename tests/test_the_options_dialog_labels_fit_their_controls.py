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
    table = source[source.index("dialog dccore.set {"):]
    return table[:table.index("\n}\n")]


CONTROL = re.compile(
    r'^\s+(check|text|button|box|edit|combo|list|radio|scroll) (?:"([^"]*)", )?(\d+), (\d+) (\d+) (\d+) (\d+)',
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
        if cid == 1682:
            texts = [PAIRED_STATUS, NOT_PAIRED_STATUS]
        # a text wraps over the lines its height holds (8 dbu a line);
        # a check or a button does not
        lines = max(1, _h // 8) if kind == "text" else 1
        available = w * PX_PER_DBU * lines - (CHECK_BOX_PX if kind == "check" else 0)
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
                kind, width, height = next((k, w, h) for k, _l, i, _x, _y, w, h in controls() if i == cid)
                # a text wraps over the lines its height holds, as labelled() counts them
                lines = max(1, height // 8) if kind == "text" else 1
                available = width * px_per_dbu * lines - (CHECK_BOX_PX if kind == "check" else 0)
                if widths[text] > available:
                    clipped.append("%s: %d %r needs %dpx, has %.1f"
                                   % (font_name, cid, text, widths[text], available))
            self.assertEqual(clipped, [])


class TheControlsStayInTheirBoxes(unittest.TestCase):
    """Nothing may hang out of the dialog; the frames are checked by
    tests/test_the_mirc_settings_window.py (a control is inside its frame
    whole, or clear of it)."""

    def test_every_control_is_inside_the_dialog(self):
        width, height = map(int, re.search(r"size -1 -1 (\d+) (\d+)", dialog_table()).groups())
        for kind, _label, cid, x, y, w, h in controls():
            if kind == "combo":
                h = 12   # the closed height; the fourth number is the dropped list
            self.assertLessEqual(x + w, width, cid)
            self.assertLessEqual(y + h, height, cid)

    def test_the_size_edit_sits_after_its_label(self):
        font = next(c for c in controls() if c[2] == 1670)
        edit = next(c for c in controls() if c[2] == 1671)
        self.assertEqual(font[1], "Fixed-width font, size")
        self.assertGreaterEqual(edit[3], font[3] + font[5])


