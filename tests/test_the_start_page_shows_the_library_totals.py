"""The control panel's start page shows the library's totals (#1359, the
script's side).

A bot with `library:1` in its CAPS sends, once the session has sent `library`,
one `DCCORE LIBRARY <files> <folders> <bytes> <rebuilt_at> <rebuild_secs>
<primary> <name>` per list and one `DCCORE SEARCHINDEX <rows> <lists> <state>`
in every status burst. The script keeps them per burst, asks once a window,
and the Library frame on the start page shows the lists with their files,
the totals, the newest rebuild and the search index - or `-` from a bot
that has sent none. mIRC cannot run here, so the script is read.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_the_mirc_settings_window import alias, generated_data, statements  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "mirc"))

import build_settings_window as generator  # noqa: E402


class TheLinesAreKeptPerBurst(unittest.TestCase):

    def test_a_library_line_is_kept_in_order_and_the_index_line_with_it(self):
        lines = statements(alias("dccore.structured"))
        self.assertIn("if (%type == LIBRARY) { if ($dccore.st(nlib) == $null) { hadd dccore.live nlib 1 } | "
                      "hadd dccore.live lib. $+ $dccore.st(nlib) $2- | hinc dccore.live nlib | return }", lines)
        self.assertIn("if (%type == SEARCHINDEX) { hadd dccore.live libindex $2- | dccore.panel.soon | return }", lines)

    def test_every_status_starts_them_afresh(self):
        lines = statements(alias("dccore.status"))
        for line in ("hdel -w dccore.live lib.*", "hdel dccore.live libindex", "hadd dccore.live nlib 1"):
            self.assertIn(line, lines)
        # cleared before the burst's lines can arrive, like the slots
        self.assertLess(lines.index("hdel -w dccore.live lib.*"), lines.index("dccore.panel.soon"))


class TheStartPageAsksOnce(unittest.TestCase):

    def test_the_ask_goes_to_a_bot_that_has_the_part_and_once_a_window(self):
        lines = statements(alias("dccore.sw.home.fill"))
        self.assertIn("if (!$dccore.sw.s(asked.library)) && ($dccore.sw.can(library)) "
                      "{ hadd dccore.sws asked.library 1 | dccore.send library }", lines)
        # after the guard that keeps the fill to the start page shown with DCX
        self.assertEqual(lines[0], "if (!$dialog(dccore.set)) || (!$dccore.sw.s(dcx)) || ($dccore.sw.s(side) != 0) { return }")

    def test_the_start_page_is_filled_as_soon_as_the_window_is_live(self):
        # the CAPS line makes the window live: the start page asks then, not
        # at the next burst up to 30 s later; a page shown is shown again
        lines = statements(alias("dccore.sw.line"))
        live = lines.index("hadd dccore.sws live 1")
        self.assertIn("if ($dccore.sw.s(side) == 0) { dccore.sw.home.fill }", lines[live:live + 5])
        self.assertIn("else { dccore.sw.page $dccore.sw.s(page) }", lines[live:live + 6])


class TheLibraryFrame(unittest.TestCase):

    def test_its_rows(self):
        data = generated_data()
        frames = {data["home.f.%d" % n].split(" ", 11)[11]: n for n in range(1, int(data["home.f.n"]) + 1)}
        number = frames["Library"]
        keys = [data["home.f.%d.r.%d" % (number, r)].split(" ", 10)[10]
                for r in range(1, int(data["home.f.%d.n" % number]) + 1)]
        self.assertEqual(keys, ["Lists", "Files", "Size", "Last rebuild", "Search index", "Rebuild", "Packing"])
        self.assertEqual(dict((title, rows) for _c, title, _k, rows in generator.STATS)["Library"], tuple(keys))

    def test_the_rows_are_filled_from_the_lines(self):
        frame = alias("dccore.sw.home.frame")
        for row, source in ((1, "$dccore.sw.home.lists"), (2, "$dccore.sw.home.lib(files)"), (3, "$dccore.sw.home.lib(size)"),
                            (4, "$dccore.sw.home.lib(rebuilt)"), (5, "$dccore.sw.home.lib(index)")):
            self.assertIn("dccore.sw.home.row $1 %d %s" % (row, source), frame)
        self.assertIn("dccore.sw.home.row $1 6 $iif($dccore.st(rebuild) != $null,$dccore.rebuild.short,idle)", frame)
        self.assertIn("dccore.sw.home.row $1 7 $iif($dccore.st(packing) != $null,", frame)

    def test_the_totals_read_the_lines_fields_as_the_bot_writes_them(self):
        lib = statements(alias("dccore.sw.home.lib"))
        # files folders bytes rebuilt_at rebuild_secs: added up, the newest rebuild kept
        self.assertEqual(lib[0], "if ($dccore.st(lib.1) == $null) { return - }")
        for line in ("inc %files $gettok(%l,1,32)", "inc %folders $gettok(%l,2,32)", "inc %bytes $gettok(%l,3,32)",
                     "if ($gettok(%l,4,32) > %at) { %at = $gettok(%l,4,32) | %secs = $gettok(%l,5,32) }"):
            self.assertIn(line, lib)
        self.assertIn("if ($1 == files) { return $dccore.num(%files) files $iif(%folders > 0,in $dccore.num(%folders) folders) }", lib)
        self.assertIn("if ($1 == size) { return $dccore.bytes(%bytes) }", lib)
        # 0 0 0 until a list is rebuilt once after the update: said so
        self.assertIn("if ($1 == rebuilt) { return $iif(%at > 0,$asctime(%at,ddd d mmm HH:nn) $+ $chr(44) took "
                      "$dccore.dur(%secs),not since the bot was updated) }", lib)
        # rows lists state: ok, filling or none
        self.assertIn("if ($gettok(%x,3,32) == none) { return no list held }", lib)
        self.assertIn("return $dccore.num($gettok(%x,1,32)) rows from $gettok(%x,2,32) lists $+ $chr(44) "
                      "$iif($gettok(%x,3,32) == filling,still filling,up to date)", lib)

    def test_a_library_size_is_said_in_terabytes(self):
        """21111GB read as a number, not a size (the operator): a TB tier,
        binary like the others, above the GB one."""
        lines = statements(alias("dccore.bytes"))
        tb = "if (%n >= 1099511627776) { return $+($dccore.round($calc(%n / 1099511627776)),TB) }"
        gb = "if (%n >= 1073741824) { return $+($dccore.round($calc(%n / 1073741824)),GB) }"
        self.assertIn(tb, lines)
        self.assertLess(lines.index(tb), lines.index(gb))

    def test_the_lists_row_names_each_list_with_its_files_when_the_lines_are_there(self):
        lists = alias("dccore.sw.home.lists")
        # primary is the sixth field, the name the rest
        self.assertIn("%name = $gettok(%l,7-,32) $iif($gettok(%l,6,32) == 1,(primary)) $dccore.num($gettok(%l,1,32)) files", lists)
        # and the Lists page's own answer otherwise, as before
        self.assertIn("if (!$dccore.sw.s(srv.ok)) {", lists)


if __name__ == "__main__":
    unittest.main()
