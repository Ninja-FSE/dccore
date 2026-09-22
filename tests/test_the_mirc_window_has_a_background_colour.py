"""#550: "I miss changing the background colour in Options."

mIRC has no per-window background COLOUR (`/color background` is for every
window at once); a custom window can only be given a background PICTURE with
`/background -t @window file`. So the option writes a small .bmp of the
chosen colour beside the script and tiles it (128x128 - see TheBitmapItWrites), and "none" removes the picture.

The script cannot be run here, so what can be checked is checked from its
source: the bytes it writes really are a valid 128x128 24-bit bitmap, the palette
is the mIRC one, and the dialog and the save handler agree about which line of
the combo is which colour.
"""

import io
import os
import re
import struct
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def script():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"),
                 encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def alias_body(source, name):
    start = source.index("alias " + name + " {")
    depth, i = 0, source.index("{", start)
    for j in range(i, len(source)):
        depth += source[j] == "{"
        depth -= source[j] == "}"
        if depth == 0:
            return source[i:j + 1]
    raise AssertionError(name)


class TheBitmapItWrites(unittest.TestCase):
    """A 128x128 tile, not one pixel: tiled, a one-pixel picture is drawn a
    pixel at a time over the whole window on every repaint, and the window
    crawled (every new line, every change in Options)."""

    SIDE = 128

    def head(self):
        body = alias_body(script(), "dccore.bgfile")
        header = re.search(r"bset &dccorebg 1 ([0-9 ]+)\r?\n", body)
        self.assertIsNotNone(header, "no header bset")
        return bytes(int(n) for n in header.group(1).split())

    def test_the_header_is_a_valid_128_square_24_bit_bitmap(self):
        head = self.head()

        self.assertEqual(len(head), 54)
        self.assertEqual(head[:2], b"BM")
        file_size, _r1, _r2, data_offset = struct.unpack("<IHHI", head[2:14])
        (dib, width, height, planes, bpp, compression, image_size,
         _xppm, _yppm, colours, important) = struct.unpack("<IiiHHIIiiII", head[14:54])
        self.assertEqual((dib, width, height, planes, bpp, compression),
                         (40, self.SIDE, self.SIDE, 1, 24, 0))
        self.assertEqual(data_offset, 54)
        # 128 pixels of three bytes is 384 a row - already a multiple of four,
        # so there is no row padding to write.
        self.assertEqual((self.SIDE * 3) % 4, 0)
        self.assertEqual(image_size, self.SIDE * self.SIDE * 3)
        self.assertEqual(file_size, 54 + image_size)
        self.assertEqual((colours, important), (0, 0))

    def test_a_row_is_128_pixels_of_blue_green_red(self):
        body = alias_body(script(), "dccore.bgfile")
        self.assertIn("var %px = $gettok(%rgb,3,46) $gettok(%rgb,2,46) $gettok(%rgb,1,46)", body)
        self.assertIn("var %row = $str(%px $chr(32),128)", body)

    def test_every_row_goes_at_its_own_offset_after_the_header(self):
        body = alias_body(script(), "dccore.bgfile")
        # mIRC binary variables count from 1: the header is bytes 1-54, the
        # first row starts at 55, and each row is 384 bytes on.
        self.assertIn("while (%y < 128) { bset &dccorebg $calc(55 + %y * 384) %row | inc %y }", body)
        self.assertEqual(len(self.head()) + 1, 55)

    def test_the_rows_are_the_size_the_header_promises(self):
        """Simulate the script's own arithmetic: 128 rows of 128 pixels of 3
        bytes fill exactly the pixel data, no gap and no overlap."""
        offsets = [55 + y * 384 for y in range(128)]
        self.assertEqual(offsets[0], 55)
        self.assertEqual(offsets[-1] + 384 - 1, 54 + self.SIDE * self.SIDE * 3)
        self.assertTrue(all(b - a == 384 for a, b in zip(offsets, offsets[1:])))

    def test_the_file_is_named_for_the_new_size(self):
        """Somebody who already ran the one-pixel version has that file; the
        new one must not be mistaken for it."""
        body = alias_body(script(), "dccore.bgfile")
        self.assertIn("dccore-bg-,$1,-128.bmp", body)

    def test_it_is_written_only_when_missing_and_only_reported_when_it_exists(self):
        body = alias_body(script(), "dccore.bgfile")
        self.assertIn("if ($isfile(%f)) { return %f }", body)
        self.assertTrue(body.rstrip().endswith("if ($isfile(%f)) { return %f }\n}") or
                        body.count("if ($isfile(%f)) { return %f }") == 2)

    def test_the_binary_variable_is_freed(self):
        self.assertIn("bunset &dccorebg", alias_body(script(), "dccore.bgfile"))


class ThePalette(unittest.TestCase):

    def entries(self):
        body = alias_body(script(), "dccore.rgb")
        listing = re.search(r"\$gettok\(([0-9. ]+),", body).group(1).split()
        return [tuple(int(n) for n in item.split(".")) for item in listing]

    def test_sixteen_colours_each_three_bytes(self):
        entries = self.entries()

        self.assertEqual(len(entries), 16)
        for entry in entries:
            self.assertEqual(len(entry), 3)
            self.assertTrue(all(0 <= n <= 255 for n in entry), entry)

    def test_the_named_ends_are_right(self):
        entries = self.entries()
        self.assertEqual(entries[0], (255, 255, 255), "00 white")
        self.assertEqual(entries[1], (0, 0, 0), "01 black")
        self.assertEqual(entries[4], (255, 0, 0), "04 red")
        self.assertEqual(entries[8], (255, 255, 0), "08 yellow")

    def test_it_lines_up_with_the_names_the_dialog_shows(self):
        names = re.search(r"alias dccore.colours \{ return (.*?) \}", script()).group(1).split(",")
        self.assertEqual(len(names), 16)
        for index, name in enumerate(names):
            self.assertTrue(name.startswith("%02d " % index), name)


class TheDialogAndTheSave(unittest.TestCase):

    def test_the_combo_is_inside_the_window_box_and_collides_with_nothing(self):
        source = script()
        dialog = source[source.index("dialog dccore.opt {"):]
        dialog = dialog[:dialog.index("\n}\n")]
        ids = [int(n) for n in re.findall(
            r'^\s+(?:check|edit|text|box|button) "[^"]*", (\d+)', dialog, re.M)]
        ids += [int(n) for n in re.findall(r"^\s+combo (\d+),", dialog, re.M)]
        self.assertGreater(len(ids), 30, "the dialog scan found almost nothing")
        self.assertEqual(len(ids), len(set(ids)), "a dialog id is used twice")

        window_box = re.search(r'box "Window", 300, (\d+) (\d+) (\d+) (\d+)', dialog)
        bx, by, bw, bh = map(int, window_box.groups())
        for pattern in (r"combo 308, (\d+) (\d+) (\d+) \d+", r'text "Background", 307, (\d+) (\d+) (\d+) \d+'):
            x, y, w = map(int, re.search(pattern, dialog).groups())
            self.assertTrue(bx <= x and x + w <= bx + bw, pattern)
            self.assertTrue(by <= y + 10 <= by + bh, pattern)

    def test_the_selected_line_and_the_saved_colour_are_the_same_mapping(self):
        """Line 1 is "none" = -1, line 2 is colour 0 ... line 17 is 15: the
        fill selects colour + 2 and the save stores line - 2, so a round trip
        through the dialog cannot move the colour."""
        source = script()
        self.assertIn("did -c dccore.opt 308 $calc($dccore.opt(bg) + 2)", source)
        self.assertIn("hadd dccore bg $calc($did(dccore.opt,308).sel - 2)", source)
        self.assertIn("did -a dccore.opt 308 none", source)

    def test_it_defaults_to_no_picture(self):
        self.assertIn("dccore.default bg -1", script())

    def test_it_is_applied_when_the_window_opens_and_when_options_are_saved(self):
        source = script()
        self.assertIn("dccore.background", alias_body(source, "dccore.window"))
        save = source[source.index("on *:dialog:dccore.opt:sclick:1:"):]
        save = save[:save.index("on *:dialog:dccore.opt:sclick:501:")]
        self.assertIn("dccore.background", save)

    def test_saving_the_options_repaints_the_picture_only_if_the_colour_changed(self):
        """Re-applying a picture repaints the whole window; an options save
        that changed the font size or a checkbox has no business doing that."""
        source = script()
        save = source[source.index("on *:dialog:dccore.opt:sclick:1:"):]
        save = save[:save.index("on *:dialog:dccore.opt:sclick:501:")]
        self.assertIn("var %bg = $dccore.opt(bg)", save)
        self.assertIn("if (%bg != $dccore.opt(bg)) { dccore.background }", save)
        # remembered before the dialog's values are read into the table
        self.assertLess(save.index("var %bg = $dccore.opt(bg)"), save.index("hadd dccore bg "))
        self.assertLess(save.index("hadd dccore bg "), save.index("if (%bg != $dccore.opt(bg))"))


class TheAlias(unittest.TestCase):

    def test_none_removes_the_picture_and_a_colour_tiles_it(self):
        body = alias_body(script(), "dccore.background")
        self.assertIn("background -x $dccore.win", body)
        self.assertIn("background -t $dccore.win $qt(%f)", body)
        self.assertIn("(%c < 0) || (%c > 15)", body)

    def test_it_does_nothing_without_the_window(self):
        body = alias_body(script(), "dccore.background")
        self.assertTrue(body.lstrip("{\n ").startswith("if (!$window($dccore.win)) { return }"))

    def test_no_colour_command_that_would_recolour_every_window(self):
        """/color background is global; using it would change the operator's
        channels and queries too."""
        self.assertNotRegex(script(), r"(?m)^\s*/?color\s+background")


class ThePanelHeadingColour(unittest.TestCase):
    """The side panel's headings (Sending, Queue, Today, Since) are drawn in
    col.head, which defaulted to navy and had no control: on a black window
    they could not be read and could not be changed. Same combo, fill and
    save as the other colours."""

    def test_the_headings_are_drawn_in_the_saved_colour(self):
        panel = alias_body(script(), "dccore.panel")
        self.assertIn("var %head = $dccore.opt(col.head)", panel)
        self.assertGreaterEqual(panel.count("aline -l %head"), 4)

    def test_the_combo_is_filled_and_saved_like_the_others(self):
        source = script()
        self.assertIn("dccore.fillcombo 218 $dccore.opt(col.head)", source)
        self.assertIn("hadd dccore col.head $calc($did(dccore.opt,218).sel - 1)", source)

    def test_it_sits_in_the_show_box_and_collides_with_nothing(self):
        source = script()
        dialog = source[source.index("dialog dccore.opt {"):]
        dialog = dialog[:dialog.index("\n}\n")]
        box = re.search(r'box "Show in @DCCore", 100, (\d+) (\d+) (\d+) (\d+)', dialog)
        bx, by, bw, bh = map(int, box.groups())
        combo = re.search(r"combo 218, (\d+) (\d+) (\d+) \d+", dialog)
        label = re.search(r'text "Panel headings", 217, (\d+) (\d+) (\d+) \d+', dialog)
        for x, y, w in (map(int, combo.groups()), map(int, label.groups())):
            self.assertTrue(bx <= x and x + w <= bx + bw)
            self.assertTrue(by <= y + 10 <= by + bh, "runs out of the box")
        # not over the status-line controls above it (edit 215 ends at y 80)
        self.assertGreater(int(label.group(2)), 69 + 11)

    def test_the_panel_is_redrawn_when_the_options_are_saved(self):
        source = script()
        save = source[source.index("on *:dialog:dccore.opt:sclick:1:"):]
        save = save[:save.index("on *:dialog:dccore.opt:sclick:501:")]
        self.assertIn("dccore.panel", save)


if __name__ == "__main__":
    unittest.main()
