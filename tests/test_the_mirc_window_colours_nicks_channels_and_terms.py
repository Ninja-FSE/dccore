"""#1259: the nick, the channel and the search term of a @DCCore line in colours of their own.

A feed line had a coloured tag and a coloured file name, and the rest plain:

    [SEARCH]   alfa in #music searched "some song" -> 3 result(s)

so on a busy window one person or one channel was hard to follow. /dccore
options now has three more colours - Search text, Nicks, Channels - and Nicks
can be "per nick": a colour of its own for each nick, from a hash of it.

Nothing may change for somebody who does not touch them. Nicks and Channels
default to "same as the line", which adds no colour codes at all; Search
text defaults to "same as File names", the colour the term already had.

mIRC cannot run here, so the script is read as source. Comment lines are
dropped first: every statement asserted on below has to be code, not a
sentence about it.
"""

import hashlib
import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")

LINE_KINDS = ("REQUEST", "QUEUED", "SENDING", "RESUMED", "SENT", "FAIL", "SEARCH")
NICK_AND_CHANNEL = "$dccore.nick($2) $+ $dccore.in($3)"


def code():
    """The script with its comment lines removed (a ";" starts a comment only
    at the start of a line in mIRC)."""
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(";"))


def block(header):
    """The block that starts with `header` (which ends in "{"), braces matched."""
    source = code()
    start = source.index(header)
    depth = 0
    for j in range(source.index("{", start), len(source)):
        depth += source[j] == "{"
        depth -= source[j] == "}"
        if depth == 0:
            return source[start:j + 1]
    raise AssertionError(header)


def alias(name):
    return block("alias %s {" % name)


def statements(body):
    """The lines of a block, stripped, without the header and the last brace."""
    return [line.strip() for line in body.split("\n")[1:-1] if line.strip()]


def handler(kind):
    body = alias("dccore.structured")
    start = body.index("if (%%type == %s) {" % kind)
    return body[start:].split("\n  if (%type ==", 1)[0]


def display_line(kind):
    """The one line of a handler that draws the event in the window."""
    lines = [line.strip() for line in handler(kind).split("\n")
             if line.strip().startswith(("dccore.msg ", "dccore.alert "))]
    assert len(lines) == 1, (kind, lines)
    return lines[0]


# --- a model of the colour arithmetic, from the script's own constants ------

def palette_rgb():
    body = alias("dccore.rgb")
    listing = re.search(r"\$gettok\(([0-9. ]+),", body).group(1).split()
    return [tuple(int(n) for n in item.split(".")) for item in listing]


def wcag_contrast(first, second):
    def channel(value):
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(rgb):
        red, green, blue = (channel(v) for v in rgb)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def nick_palettes():
    """The sixteen lists written in dccore.nickpal, one per background."""
    found = re.search(r"return \$gettok\(([0-9 /]+),\$calc\(%bg \+ 1\),47\)", alias("dccore.nickpal"))
    assert found, "no palette table in dccore.nickpal"
    return [entry.split() for entry in found.group(1).split("/")]


def hex_digits_used():
    found = re.search(r"\$base\(\$left\(\$md5\(\$lower\(\$1\)\),(\d+)\),16,10\)", alias("dccore.pernick"))
    assert found, "dccore.pernick does not hash the lower-cased nick"
    return int(found.group(1))


def per_nick_colour(nick, background):
    """What dccore.pernick answers, the same steps in Python: MD5 of the nick
    in lower case, its first hex digits as a number, that modulo the length of
    the background's list, plus one, as a token of that list."""
    palette = nick_palettes()[background]
    number = int(hashlib.md5(nick.lower().encode("ascii")).hexdigest()[:hex_digits_used()], 16)
    return palette[(number % len(palette) + 1) - 1]


class TheDefaultsChangeNothing(unittest.TestCase):

    def init(self):
        return statements(alias("dccore.init"))

    def test_nicks_and_channels_default_to_same_as_the_line(self):
        self.assertIn("dccore.default col.nick -1", self.init())
        self.assertIn("dccore.default col.chan -1", self.init())

    def test_the_search_text_defaults_to_the_file_names_colour(self):
        """What it had before: the term went through dccore.name."""
        self.assertIn("dccore.default col.term name", self.init())
        self.assertIn("if ($dccore.opt(col.term) == name) { return $dccore.name($1-) }",
                      statements(alias("dccore.term")))

    def test_a_saved_choice_is_never_overwritten(self):
        """Through dccore.default, which sets a key only when nothing is saved."""
        for key in ("col.term", "col.nick", "col.chan"):
            self.assertNotRegex(alias("dccore.init"), r"hadd dccore %s " % re.escape(key))
        self.assertIn("if ($hget(dccore,$1) == $null) { hadd dccore $1 $2- }", alias("dccore.default"))

    def test_same_as_the_line_adds_no_codes_at_all(self):
        """Not even a colour reset: a stray ^O would end a span the line has."""
        body = statements(alias("dccore.paint"))
        self.assertEqual(body[0], "if ($1 !isnum 0-15) { return $2 }")
        self.assertEqual(len(body), 2)


class TheHelpers(unittest.TestCase):

    def test_a_colour_is_always_two_digits(self):
        """^C3 then "3bot" reads as colour 33: the number is padded to two."""
        self.assertEqual(statements(alias("dccore.paint"))[1],
                         "return $+($chr(3),$base($1,10,10,2),$2,$chr(15))")

    def test_every_colour_code_in_the_new_helpers_is_padded(self):
        for name in ("dccore.paint", "dccore.nick", "dccore.chan", "dccore.term", "dccore.pernick"):
            body = alias(name)
            for match in re.finditer(r"\$chr\(3\),", body):
                self.assertRegex(body[match.end():], r"^(\$base\(\$1,10,10,2\)|\$dccore\.col\()", name)

    def test_the_palette_is_written_in_two_digits(self):
        for entry in nick_palettes():
            for colour in entry:
                self.assertRegex(colour, r"^\d\d$")

    def test_the_nick_takes_its_colour_or_its_own_per_nick(self):
        self.assertEqual(statements(alias("dccore.nick")), [
            "var %c = $dccore.opt(col.nick)",
            "if (%c == per) { %c = $dccore.pernick($1) }",
            "return $dccore.paint(%c,$1)",
        ])

    def test_the_channel_takes_its_colour(self):
        self.assertIn("alias dccore.chan { return $dccore.paint($dccore.opt(col.chan),$1) }", code())

    def test_the_term_is_quoted_in_every_colour(self):
        self.assertIn('return $dccore.paint($dccore.opt(col.term),$+(",$1-,"))', alias("dccore.term"))

    def test_the_channel_goes_through_dccore_in_still_non_breaking(self):
        line = code().split("alias dccore.in {", 1)[1].split("\n", 1)[0]
        self.assertIn("return $+($chr(160),in,$chr(160),$dccore.chan($1))", line)
        self.assertIn("if ($1 == $null) || ($1 == -) { return }", line)


class EveryLineThatNamesSomebody(unittest.TestCase):

    def test_each_kind_draws_the_nick_and_channel_through_the_helpers(self):
        for kind in LINE_KINDS:
            self.assertIn(NICK_AND_CHANNEL, display_line(kind), kind)

    def test_no_raw_nick_is_left_on_those_lines(self):
        for kind in LINE_KINDS:
            rest = display_line(kind).replace("$dccore.nick($2)", "")
            self.assertNotRegex(rest, r"\$2(?![\d-])", kind)

    def test_the_channel_is_reached_only_through_dccore_in(self):
        for kind in LINE_KINDS:
            rest = display_line(kind).replace("$dccore.in($3)", "")
            self.assertNotRegex(rest, r"\$3(?![\d-])", kind)

    def test_dccore_in_is_never_used_without_the_nick_helper(self):
        source = code()
        self.assertEqual(source.count("$dccore.in("), source.count(NICK_AND_CHANNEL))
        self.assertGreaterEqual(source.count(NICK_AND_CHANNEL), len(LINE_KINDS))

    def test_the_search_term_has_its_own_helper(self):
        line = display_line("SEARCH")
        self.assertIn("searched $dccore.term($5-) ->", line)
        self.assertNotIn("$dccore.name(", line)

    def test_a_file_name_keeps_the_file_names_colour(self):
        self.assertIn("$dccore.name($5-)", display_line("REQUEST"))
        for kind in ("QUEUED", "SENDING", "RESUMED", "SENT", "FAIL"):
            self.assertIn("$dccore.name(", display_line(kind), kind)
            self.assertNotIn("$dccore.term(", display_line(kind), kind)


class PerNick(unittest.TestCase):

    def test_it_hashes_the_nick_in_lower_case(self):
        """$lower first, so Alfa and ALFA are one colour."""
        self.assertEqual(hex_digits_used(), 6)
        self.assertIn("return $gettok(%pal,$calc((%n % $numtok(%pal,32)) + 1),32)", alias("dccore.pernick"))

    def test_the_same_nick_is_the_same_colour_in_any_case(self):
        for background in range(16):
            self.assertEqual(per_nick_colour("Alfa", background), per_nick_colour("alfa", background))
            self.assertEqual(per_nick_colour("ALFA", background), per_nick_colour("alfa", background))
            self.assertEqual(per_nick_colour("bravo", background), per_nick_colour("bravo", background))

    def test_nicks_are_spread_over_the_palette(self):
        nicks = ["alfa", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
                 "india", "juliett", "kilo", "lima", "mike", "november", "oscar", "papa"]
        colours = {per_nick_colour(nick, 0) for nick in nicks}
        self.assertGreaterEqual(len(colours), 5, colours)

    def test_each_list_is_what_the_contrast_rule_gives(self):
        """Never white or black (one is the line's own text colour), never the
        background, and at least 3:1 against it - recomputed from dccore.rgb."""
        rgb = palette_rgb()
        palettes = nick_palettes()
        self.assertEqual(len(palettes), 16)
        for background in range(16):
            expected = ["%02d" % colour for colour in range(2, 16)
                        if colour != background and wcag_contrast(rgb[colour], rgb[background]) >= 3]
            self.assertEqual(palettes[background], expected, "background %d" % background)
            self.assertTrue(palettes[background], "background %d has no readable colour" % background)

    def test_no_list_holds_its_own_background_white_or_black(self):
        for background, entry in enumerate(nick_palettes()):
            self.assertNotIn("%02d" % background, entry)
            self.assertNotIn("00", entry)
            self.assertNotIn("01", entry)

    def test_the_background_is_the_windows_or_mircs_own(self):
        self.assertEqual(statements(alias("dccore.nickpal"))[:3], [
            "var %bg = $dccore.opt(bg)",
            "if (%bg !isnum 0-15) { %bg = $color(background) }",
            "if (%bg !isnum 0-15) { %bg = 0 }",
        ])


class TheOptionsDialog(unittest.TestCase):

    COMBOS = (("col.term", 220, "Search text", 221, "name -1"),
              ("col.nick", 222, "Nicks", 223, "-1 per"),
              ("col.chan", 224, "Channels", 225, "-1"))

    def table(self):
        return block("dialog dccore.opt {")

    def geometry(self, pattern):
        return tuple(int(n) for n in re.search(pattern, self.table()).groups())

    def test_the_three_combos_are_in_the_show_box_under_the_checks(self):
        bx, by, bw, bh = self.geometry(r'box "Show in @DCCore", 100, (\d+) (\d+) (\d+) (\d+)')
        _x, last_check_y, _w, last_check_h = self.geometry(r'check "Other log lines", 108, (\d+) (\d+) (\d+) (\d+)')
        for _key, text_id, label, combo_id, _own in self.COMBOS:
            tx, ty, tw, th = self.geometry(r'text "%s", %d, (\d+) (\d+) (\d+) (\d+)' % (label, text_id))
            cx, cy, cw = self.geometry(r"combo %d, (\d+) (\d+) (\d+) \d+, drop" % combo_id)
            for x, y, w in ((tx, ty, tw), (cx, cy, cw)):
                self.assertTrue(bx <= x and x + w <= bx + bw, label)
                self.assertTrue(by <= y and y + 12 <= by + bh, label)
                self.assertGreaterEqual(y, last_check_y + last_check_h, label)
            # the label above its combo, in the same column
            self.assertEqual(tx, cx, label)
            self.assertLessEqual(ty + th, cy, "the label runs into its combo: " + label)

    def test_the_columns_do_not_overlap(self):
        spans = sorted(self.geometry(r"combo %d, (\d+) \d+ (\d+) \d+, drop" % combo_id)
                       for _key, _text_id, _label, combo_id, _own in self.COMBOS)
        for (x, w), (next_x, _w) in zip(spans, spans[1:]):
            self.assertLess(x + w, next_x)

    def test_init_fills_each_from_its_setting(self):
        init = statements(block("on *:dialog:dccore.opt:init:0: {"))
        for key, _text_id, _label, combo_id, own in self.COMBOS:
            self.assertIn("dccore.fillspan %d $dccore.opt(%s) %s" % (combo_id, key, own), init)

    def test_ok_saves_each_with_the_same_lines_it_was_filled_with(self):
        ok = statements(block("on *:dialog:dccore.opt:sclick:1: {"))
        saved = ok.index("dccore.save")
        for key, _text_id, _label, combo_id, own in self.COMBOS:
            line = "hadd dccore %s $dccore.spanval(%d,%s)" % (key, combo_id, own)
            self.assertIn(line, ok)
            self.assertLess(ok.index(line), saved, line)

    def test_the_fill_and_the_read_back_are_one_mapping(self):
        """N lines of its own, then colour C on line C + N + 1; read back the
        other way. A round trip through the dialog cannot move a choice."""
        fill = statements(alias("dccore.fillspan"))
        back = statements(alias("dccore.spanval"))
        self.assertIn("var %at = $findtok(%own,$2,1,32)", fill)
        self.assertIn("if (!%at) { %at = $iif($2 isnum 0-15,$calc($2 + %n + 1),1) }", fill)
        self.assertIn("did -c dccore.opt $1 %at", fill)
        self.assertIn("if (%sel <= %n) { return $gettok($2,$iif(%sel < 1,1,%sel),32) }", back)
        self.assertIn("return $calc(%sel - %n - 1)", back)

        def selected(saved, own):
            own = own.split()
            if saved in own:
                return own.index(saved) + 1
            return int(saved) + len(own) + 1 if saved.isdigit() and int(saved) <= 15 else 1

        def read_back(line, own):
            own = own.split()
            return own[max(line, 1) - 1] if line <= len(own) else str(line - len(own) - 1)

        for _key, _text_id, _label, _combo_id, own in self.COMBOS:
            for saved in own.split() + [str(c) for c in range(16)]:
                self.assertEqual(read_back(selected(saved, own), own), saved, (own, saved))

    def test_the_lines_of_their_own_say_what_they_mean(self):
        body = statements(alias("dccore.spanlabel"))
        self.assertEqual(body, [
            "if ($1 == name) { return same as File names }",
            "if ($1 == per) { return per nick }",
            "return same as the line",
        ])
        for line in body:
            self.assertNotIn(",", line.split("return", 1)[1], "a comma would split the did -a")


class TheVersion(unittest.TestCase):

    def test_the_script_is_at_least_1_18_0(self):
        """The changelog tells operators to update to 1.18.0 for this."""
        found = re.search(r"(?m)^alias dccore\.ver \{ return (\d+)\.(\d+)\.(\d+) \}$", code())
        self.assertIsNotNone(found)
        self.assertGreaterEqual(tuple(int(part) for part in found.groups()), (1, 18, 0))


if __name__ == "__main__":
    unittest.main()
