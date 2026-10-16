"""dccore.mrc uses DCX only when it can, and says why when it cannot (#1288).

mIRC cannot be run here, so these read the script: every reason the built-in
window is used must be in $dccore.dcx.why, in an order where a cheap check
answers before the dll is touched, and a dll that will not load must not stop
the script.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_the_mirc_settings_window import (alias, block, code, dialog, generated_data, handwritten, raw,  # noqa: E402
                                           statements, untext)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "mirc"))

import settings_window_layout as layout  # noqa: E402


class WhyNot(unittest.TestCase):

    def setUp(self):
        self.why = statements(alias("dccore.dcx.why"))
        self.body = "\n".join(self.why)

    def position(self, needle):
        self.assertIn(needle, self.body)
        return self.body.index(needle)

    def test_every_reason_is_there(self):
        for needle in ("$dccore.opt(nodcx)", "$version < 7.45", "$bits == 64",
                       "!$isfile($dccore.dcx.file)", "$sha256($dccore.dcx.file,2) != $dccore.dcx.sha256",
                       "$dccore.dcx.version == $null"):
            self.position(needle)

    def test_the_dll_is_touched_last(self):
        # The option, the version, the bitness, the file and its hash all
        # answer without loading anything; only then is the dll asked.
        order = [self.position(n) for n in ("$dccore.opt(nodcx)", "$version < 7.45", "$bits == 64",
                                            "!$isfile(", "$sha256(", "$dccore.dcx.version == $null")]
        self.assertEqual(order, sorted(order))

    def test_a_loader_that_is_not_the_shipped_one_stops_it_but_a_missing_one_does_not(self):
        self.assertIn("if ($isfile($dccore.dcx.wv2file)) && ($sha256($dccore.dcx.wv2file,2) != "
                      "$dccore.dcx.wv2sha256)", self.body)
        self.assertIn("$scriptdir $+ dcx\\WebView2Loader.dll", alias("dccore.dcx.wv2file"))
        self.assertLess(self.position("$sha256($dccore.dcx.file,2)"), self.position("$sha256($dccore.dcx.wv2file,2)"))
        self.assertLess(self.position("$sha256($dccore.dcx.wv2file,2)"), self.position("$dccore.dcx.version == $null"))

    def test_a_dll_another_script_loaded_is_not_hash_checked(self):
        guard = block("if (!$dccore.dcx.loaded) {", "\n".join(self.why))
        self.assertIn("$isfile(", guard)
        self.assertIn("$sha256(", guard)
        self.assertNotIn("$dccore.dcx.version", guard)

    def test_success_says_nothing(self):
        # An empty answer is what "use DCX" means to every caller.
        self.assertFalse([s for s in self.why if s.startswith("return") and len(s.split()) == 1])
        self.assertTrue(all("{ return " in s for s in self.why if s.startswith("if (") and "{" in s
                            and not s.endswith("{")))


class TheLoadCannotStopTheScript(unittest.TestCase):

    def test_the_version_call_traps_its_error(self):
        lines = statements(alias("dccore.dcx.version"))
        self.assertTrue(lines[0].startswith("return $dll($dccore.dcx.path,Version,"))
        self.assertIn(":error", lines)
        self.assertIn("reseterror", lines)
        self.assertLess(lines.index(":error"), lines.index("reseterror"))

    def test_the_dll_is_always_called_by_its_full_path(self):
        body = alias("dccore.dcx")
        self.assertIn("$dll($dccore.dcx.path,$1,$2-)", body)
        self.assertIn("dll $qt($dccore.dcx.path) $1 $2-", body)
        # Never by bare name, which would pick up whatever dcx.dll mIRC finds.
        self.assertNotRegex(code(), r"(?m)^\s*dll\s+\"?dcx\.dll")

    def test_no_users_dcx_aliases_are_relied_on(self):
        # xdid/xdialog/dcx are what DCX's own helper script defines; a user
        # may have them, or other versions of them. Ours goes through dccore.dcx.
        for line in code().split("\n"):
            first = line.strip().split(" ", 1)[0]
            self.assertNotIn(first, ("xdid", "xdialog", "dcx"), line)

    def test_the_path_is_beside_the_script(self):
        self.assertIn("$scriptdir $+ dcx\\dcx.dll", alias("dccore.dcx.file"))
        self.assertIn("$dll(dcx.dll)", alias("dccore.dcx.loaded"))


class TheOption(unittest.TestCase):

    def test_it_defaults_to_dcx(self):
        self.assertIn("dccore.default nodcx 0", alias("dccore.init"))

    def test_it_is_on_the_script_tabs_window_page(self):
        pages = {name: sections for _group, ps in layout.GROUPS for name, sections in ps}
        items = [item for _title, section in pages["Window"] for item in section]
        self.assertIn({"local": "nodcx", "label": "Use the built-in settings window, not DCX"}, items)

    def test_the_generated_window_carries_it(self):
        self.assertRegex(raw(), r"(?m)^  hadd dccore\.swm lo\.nodcx \d+$")


class TheCommand(unittest.TestCase):

    def test_dccore_dcx_and_check(self):
        branch = block("if (%cmd == dcx) {", alias("dccore"))
        self.assertIn("if ($2 == check) { dccore.dcx.check }", branch)
        self.assertIn("else { dccore.dcx.status }", branch)

    def test_the_help_lists_it(self):
        self.assertIn("/dccore dcx [check]", alias("dccore"))

    def test_the_status_says_why_not(self):
        body = alias("dccore.dcx.status")
        self.assertIn("var %why = $dccore.dcx.why", body)
        self.assertIn("DCX is not used: %why", body)

    def test_the_check_is_refused_when_dcx_cannot_be_used(self):
        lines = statements(alias("dccore.dcx.check"))
        self.assertEqual(lines[0], "var %why = $dccore.dcx.why")
        self.assertTrue(lines[1].startswith("if (%why != $null) {") and "return" in lines[1])


class TheCheckWindow(unittest.TestCase):

    def test_it_holds_a_native_button_and_the_box_the_tree_should_fill(self):
        table = block("dialog dccore.dcxcheck {")
        self.assertIn("option dbu", table)
        self.assertRegex(table, r'button "[^"]+", 1, ')
        self.assertRegex(table, r'box "[^"]+", 2, 110 4 126 90')

    def test_mark_comes_before_any_dcx_control(self):
        lines = statements(alias("dccore.dcx.checkinit"))
        self.assertEqual(lines[0], "dccore.dcx Mark dccore.dcxcheck dccore.dcx.checkcb")
        dcx = [i for i, line in enumerate(lines) if line.startswith("dccore.dcx x")]
        self.assertTrue(dcx and min(dcx) > 0)

    def test_the_tree_is_placed_inside_the_box_by_the_reported_scale(self):
        body = alias("dccore.dcx.checkinit")
        self.assertIn("$dialog(dccore.dcxcheck).cw", body)
        self.assertIn("$calc(%cw / 240)", body)
        self.assertIn("-c 10 treeview %x %y %w %h", body)
        # 114,14 118x76 sits inside the box at 110,4 126x90, clear of its label.
        found = re.search(r"\$calc\((\d+) \* %sx\).*\$calc\((\d+) \* %sy\).*\$calc\((\d+) \* %sx\)"
                          r".*\$calc\((\d+) \* %sy\)", body)
        x, y, w, h = (int(n) for n in found.groups())
        self.assertTrue(110 < x and x + w < 110 + 126 and 4 + 8 <= y and y + h < 4 + 90)

    def test_both_events_reach_atdccore(self):
        self.assertIn("on *:dialog:dccore.dcxcheck:sclick:1:", code())
        self.assertIn("if ($2 == sclick) && ($3 == 10)", alias("dccore.dcx.checkcb"))


class ThePageList(unittest.TestCase):
    """Phase 2 and 3: the built-in window, marked, with the mockup's
    navigation - the tab strip for the groups, and a DCX list of the chosen
    tab's pages where the page buttons are."""

    def test_it_is_built_before_the_first_page_is_shown(self):
        lines = statements(alias("dccore.sw.init"))
        # with DCX the window opens on its start page (or the mIRC side, asked
        # for); the native form on its first tab (or the mIRC tab, the last)
        self.assertLess(lines.index("dccore.sw.dcx"),
                        lines.index("if ($dccore.sw.s(dcx)) { dccore.sw.side $iif($hget(dccore.live,sw.open) == mirc,2,0) }"))
        self.assertIn("else { dccore.sw.tab $iif($hget(dccore.live,sw.open) == mirc,$dccore.sw.m(groups),1) }", lines)

    def test_nothing_happens_when_dcx_cannot_be_used(self):
        lines = statements(alias("dccore.sw.dcx"))
        self.assertEqual(lines[0], "if ($dccore.dcx.why != $null) { return }")
        self.assertEqual(lines[1], "dccore.dcx Mark dccore.set dccore.sw.dcxcb")
        self.assertEqual(lines[2], "hadd dccore.sws dcx 1")

    def test_the_buttons_and_the_native_tabs_are_hidden_for_it(self):
        body = alias("dccore.sw.dcx")
        self.assertIn("did -h dccore.set $calc(1019 + %i)", body)
        # the tab strip is the script's own with DCX (every line in one
        # colour): the native tabs are hidden, so a jump to a page selects
        # its native tab only in the native form
        self.assertIn("did -h dccore.set $calc(1000 + %i)", body)
        self.assertIn("if (!$dccore.sw.s(dcx)) { did -v dccore.set $calc(1019 + %i) }", alias("dccore.sw.tab"))
        self.assertIn("if (!$dccore.sw.s(dcx)) { did -f dccore.set $calc(1000 + %g) }", alias("dccore.sw.goto"))

    def test_it_is_placed_by_the_generated_geometry_and_the_reported_scale(self):
        body = alias("dccore.sw.dcx")
        rect = alias("dccore.sw.dcxrect")
        self.assertIn("$dialog(dccore.set).cw / $gettok($dccore.sw.m(size),1,32)", rect)
        self.assertIn("$dialog(dccore.set).ch / $gettok($dccore.sw.m(size),2,32)", rect)
        self.assertIn("var %t = $dccore.sw.m(dcxpages), %id = $gettok(%t,1,32), %rect = "
                      "$dccore.sw.dcxrect($gettok(%t,2-5,32))", body)
        self.assertIn("-c %id listview %rect report noheader singlesel fullrow showsel", body)
        # the four numbers reach the identifier as ONE parameter (mIRC splits
        # an identifier's parameters on commas): read as $1 to $4 they made
        # every DCX control zero-sized, so they are taken apart from $1-
        self.assertIn("var %r = $1-,", rect)
        for n in (1, 2, 3, 4):
            self.assertIn("$gettok(%%r,%d,32)" % n, rect)
        self.assertNotRegex(rect, r"\$[1-4] \*")
        found = re.search(r"(?m)^  hadd dccore\.swm dcxpages (\d+) (\d+) (\d+) (\d+) (\d+)$", raw())
        size = re.search(r"(?m)^  hadd dccore\.swm size (\d+) (\d+)$", raw())
        self.assertTrue(found and size)
        list_id, x, y, w, h = (int(n) for n in found.groups())
        import build_settings_window as builder
        self.assertEqual((int(size.group(1)), int(size.group(2))), (builder.DIALOG_W, builder.DIALOG_H))
        self.assertLessEqual(x + w, builder.CONTENT_X - 2)
        # inside the tab control's frame, under the tabs themselves
        self.assertGreaterEqual(y, builder.TABS[1] + builder.TAB_HEADER_H)
        self.assertLessEqual(y + h, builder.TABS[1] + builder.TABS[3] - 2)
        self.assertGreater(x, builder.TABS[0])
        table = block("dialog dccore.set {", raw())
        self.assertNotIn(", %d, " % list_id, table)

    def test_the_chosen_tabs_pages_fill_it_from_the_window_data(self):
        self.assertIn("if ($dccore.sw.s(dcx)) { dccore.sw.dcxpages %pages | dccore.sw.dcxtabsel }", alias("dccore.sw.tab"))
        lines = statements(alias("dccore.sw.dcxpages"))
        self.assertIn("dccore.dcx xdid dccore.set %id -r", lines)
        # each row with its page's icon (#1368): 0, none, without the icon file
        self.assertIn("dccore.dcx xdid dccore.set %id -a 0 0 + $dccore.theme.pageicon($gettok($1-,%i,32)) 0 0 0 0 0 "
                      "$dccore.sw.untext($dccore.sw.m(p. $+ $gettok($1-,%i,32)))", lines)
        # refilled quietly: emptying and filling the list is not a click
        self.assertLess(lines.index("hadd dccore.sws dcxquiet 1"), lines.index("dccore.dcx xdid dccore.set %id -r"))
        self.assertEqual(lines[-1], "hdel dccore.sws dcxquiet")

    def test_the_page_shown_is_selected_quietly(self):
        self.assertIn("if ($dccore.sw.s(dcx)) { dccore.sw.dcxshow $1 }", alias("dccore.sw.page"))
        lines = statements(alias("dccore.sw.dcxshow"))
        quiet = lines.index("hadd dccore.sws dcxquiet 1")
        select = [i for i, line in enumerate(lines) if " -c %at" in line][0]
        self.assertLess(quiet, select)
        self.assertLess(select, lines.index("hdel dccore.sws dcxquiet"))

    def test_a_click_on_a_page_drives_the_same_switch_as_its_button(self):
        lines = statements(alias("dccore.sw.dcxcb"))
        self.assertEqual(lines[0], "if ($dccore.sw.s(dcxquiet)) { return }")
        self.assertIn("if ($3 != $gettok($dccore.sw.m(dcxpages),1,32)) || (!$istok(sclick selected,$2,32)) || "
                      "($4 !isnum) { return }", lines)
        self.assertIn("var %p = $gettok($dccore.sw.m(g. $+ $dccore.sw.s(tab) $+ .pages),$4,32)", lines)
        self.assertIn("if (%p == $null) || (%p == $dccore.sw.s(page)) { return }", lines)
        # through dccore.sw.go, which asks first when something is not saved
        self.assertEqual(lines[-1], "dccore.sw.go page %p")


TABLES = (1520, 1540, 1580, 1595)


class TheMockupLayout(unittest.TestCase):
    """Phase 3: the pages as the mockup draws them - each section a titled
    frame, every label above its field, the page's name over the page - in
    one window that both DCX and the built-in controls draw."""

    @classmethod
    def setUpClass(cls):
        import build_settings_window as builder
        cls.builder = builder
        cls.controls = dialog("dccore.set")
        cls.where = {c[2]: c for c in cls.controls}
        cls.order = {c[2]: n for n, c in enumerate(cls.controls)}
        cls.data = generated_data()
        cls.pages = {}
        for page in range(1, int(cls.data["pages"]) + 1):
            ids = []
            for part in range(1, int(cls.data["p.%d.n" % page]) + 1):
                ids.extend(int(i) for i in cls.data["p.%d.%d" % (page, part)].split(",") if i)
            cls.pages[page] = ids

    def test_every_titled_section_is_a_frame_listed_before_what_it_holds(self):
        frames = [c for c in self.controls if c[0] == "box"]
        self.assertGreater(len(frames), 40)
        for _kind, title, fid, x, y, w, h, _style in frames:
            self.assertTrue(title, fid)
            held = [c for c in self.controls if c[2] != fid and c[0] != "box"
                    and x <= c[3] and c[3] + c[5] <= x + w and y <= c[4] and c[4] + c[6] <= y + h
                    and self.page_of(c[2]) == self.page_of(fid)]
            self.assertTrue(held, "the frame %d (%r) holds nothing" % (fid, title))
            for c in held:
                # mIRC draws a control over the ones listed before it: a frame
                # listed after its controls would cover them
                self.assertLess(self.order[fid], self.order[c[2]], "%d is listed before its frame %d" % (c[2], fid))

    def page_of(self, cid):
        return next((page for page, ids in self.pages.items() if cid in ids), None)

    def test_labels_sit_above_their_fields_except_on_the_compact_page(self):
        layouts = {page: self.data["p.%d.layout" % page] for page in self.pages}
        compact = [untext(self.data["p.%d" % page]) for page, form in layouts.items() if form.startswith("compact")]
        self.assertEqual(compact, ["File locations"])
        self.assertIn("roomy 2", layouts.values())
        above = beside = 0
        for name, value in self.data.items():
            if not name.startswith("k.") or name == "k.CHANNEL":
                continue
            cid, kind, _factor, label_id = value.split()
            if kind == "bool":
                continue
            label, control = self.where[int(label_id)], self.where[int(cid)]
            if layouts[self.page_of(int(cid))].startswith("roomy"):
                self.assertEqual(label[3], control[3], name)
                self.assertLessEqual(label[4] + label[6], control[4], name)
                if kind not in ("choice", "tri", "colour"):
                    self.assertGreaterEqual(control[5], label[5] - self.builder.BROWSE_W - 2, name)
                above += 1
            else:
                self.assertLess(label[3] + label[5], control[3], name)
                beside += 1
        self.assertGreater(above, 90)
        self.assertEqual(beside, 23)

    def test_a_check_that_fits_no_column_sends_the_page_to_the_next_form_not_an_error(self):
        # Fetch tuning is two roomy columns only because its re-fetch check
        # was shortened to fit one; the long one made the page compact
        page = next(p for p in self.pages if untext(self.data["p.%d" % p]) == "Fetch tuning")
        self.assertEqual(self.data["p.%d.layout" % page], "roomy 2")
        import settings_window_layout as layout
        fetch = dict(dict(layout.GROUPS)["Downloads"])["Fetch tuning"]
        labels = [item["label"] for _h, items in fetch for item in items if isinstance(item, dict) and "label" in item]
        self.assertIn("Re-fetch when a bot has a new list", labels)

    def test_the_title_sits_between_the_tabs_and_the_page(self):
        tid, x, y, w, h = (int(n) for n in self.data["dcxtitle"].split())
        self.assertEqual(tid, self.builder.DCX_TITLE_ID)
        self.assertGreaterEqual(y, self.builder.TABS[1] + self.builder.TAB_HEADER_H)
        self.assertLessEqual(y + h, self.builder.CONTENT_Y)
        self.assertGreaterEqual(x, self.builder.CONTENT_X)
        body = alias("dccore.sw.dcx")
        self.assertIn("-c $gettok(%t,1,32) text $dccore.sw.dcxrect($gettok(%t,2-5,32)) nowrap noprefix noformat", body)
        # bold, its flags from dccore.sw.fontflags: "+ab" without ClearType (#1382)
        self.assertIn("dccore.dcx xdid dccore.set $gettok(%t,1,32) -f $dccore.sw.fontflags(b) default 9 Tahoma", body)
        show = alias("dccore.sw.showpage")
        self.assertIn("if ($2 == -v) { dccore.dcx xdid dccore.set $gettok($dccore.sw.m(dcxtitle),1,32) -t "
                      "$dccore.sw.untext($dccore.sw.m(p. $+ $1)) }", show)

    def test_the_windows_lines_are_the_scripts_own_in_one_colour(self):
        """Windows draws the tabs and the frames in light greys of its own,
        and the first tab with no left edge at all; with DCX the script
        draws every line itself, in the system's shadow colour, and hides
        the native tabs and frames. The native form keeps Windows' look."""
        b = self.builder
        body = alias("dccore.sw.dcx")
        # through dccore.theme.sys (#1368): the system's colours in the light
        # theme, exactly as before, the theme's own in the dark one
        system = alias("dccore.theme.sys")
        for name, attribute in (("line", "COLOR_3DSHADOW"), ("face", "COLOR_3DFACE"), ("light", "COLOR_3DLIGHT")):
            self.assertIn("hadd dccore.sws %s $dccore.theme.sys(%s)" % (name, name), body)
            self.assertIn("if ($1 == %s) { return $dccore.dcx(GetSystemColor,%s) }" % (name, attribute), system)
        self.assertEqual(statements(system)[0], "if ($dccore.theme.dialogsdark) { return $dccore.theme.rgb($1) }")
        self.assertIn("while (%i <= $dccore.sw.m(groups)) { did -h dccore.set $calc(1000 + %i) | inc %i }", body)
        lines = statements(body)
        # the chosen tab stands on the body frame's top line: made after it
        self.assertLess(lines.index("dccore.sw.dcxring $gettok(%b,1,32) $dccore.sw.dcxrect($gettok(%b,2-5,32))"),
                        lines.index("dccore.sw.dcxtabs"))
        # a page's frames are made the first time it is shown, not here (#1382)
        self.assertFalse([s for s in lines if "dccore.sw.dcxframes" in s], lines)
        line = alias("dccore.sw.dcxline")
        self.assertIn("-c $1 panel $2 $3 $4 $5", line)
        self.assertIn("-C +b $dccore.sw.s(line)", line)
        # a frame is one panel shaped as a ring: its region the inside first,
        # then the outside less that - a combined region is the new one less
        # the old - so the controls inside stay Windows' own
        ring = statements(alias("dccore.sw.dcxring"))
        self.assertEqual(ring, ["dccore.sw.dcxline $1 $2 $3 $4 $5",
                                "dccore.dcx xdid dccore.set $1 -R +p $dccore.sw.dcxpoly($4,$5,i,4)",
                                "dccore.dcx xdid dccore.set $1 -R +ip $dccore.sw.dcxpoly($4,$5,o,4)"])
        # the polygon: the generator's top-left corner mirrored to the other
        # three, in the order round the shape; two corners for a tab, with
        # a straight bottom on the outside's edge or the inside's
        poly = statements(alias("dccore.sw.dcxpoly"))
        self.assertIn("%tl = %tl $dccore.sw.dcxpt(%x,%y)", poly)
        self.assertIn("%tr = $dccore.sw.dcxpt($calc($1 - %x),%y) %tr", poly)
        self.assertIn("%br = %br $dccore.sw.dcxpt($calc($1 - %x),$calc($2 - %y))", poly)
        self.assertIn("%bl = $dccore.sw.dcxpt(%x,$calc($2 - %y)) %bl", poly)
        self.assertEqual(poly[-1], "return %tl %tr %br %bl")
        self.assertIn("if ($4 == 2) { var %e = $iif($3 == i,1,0) | return $dccore.sw.dcxpt(%e,$2) %tl %tr "
                      "$dccore.sw.dcxpt($calc($1 - %e),$2) }", poly)
        outer, inner = b.corner_traces(b.ROUND_PX)
        self.assertEqual(self.data["dcxcorner.o"], " ".join("%d,%d" % p for p in outer))
        self.assertEqual(self.data["dcxcorner.i"], " ".join("%d,%d" % p for p in inner))
        # the body frame: the tab control's area under the tabs
        first, x, y, w, h = (int(n) for n in self.data["dcxbody"].split())
        self.assertEqual((first, x, y, w, h),
                         (b.BODY_DCX_FIRST, b.TABS[0], b.TABS[1] + b.TAB_HEADER_H, b.TABS[2], b.TABS[3] - b.TAB_HEADER_H))
        self.assertTrue(b.BODY_DCX_FIRST + 1 <= b.DCX_PAGES_ID)
        # nothing of it in the native form, where the tabs still take the focus
        table = block("dialog dccore.set {", raw())
        for key in ("dcxbody", "dcxtabs", "dcxring", "dcxcorner"):
            self.assertNotIn(key, table)
        self.assertIn("if (!$dccore.sw.s(dcx)) { did -f dccore.set $calc(1000 + %g) }", alias("dccore.sw.goto"))
        stay = statements(alias("dccore.sw.stay"))
        self.assertLess(stay.index("if ($dccore.sw.s(dcx)) { dccore.sw.dcxshow $dccore.sw.s(page) | return }"),
                        stay.index("did -f dccore.set $calc(1000 + $dccore.sw.s(tab))"))

    def test_the_tab_strip_is_the_tabs_side_by_side_on_the_frames_top_line(self):
        b = self.builder
        first, stride, top, height, lift, count = (int(n) for n in self.data["dcxtabs"].split())
        self.assertEqual((first, stride, top, height, lift),
                         (b.TAB_DCX_FIRST, b.TAB_DCX_STRIDE, b.TABS[1], b.TAB_HEADER_H, b.TAB_LIFT))
        # the bot's groups have tabs; the last group is the mIRC side, no strip
        groups = [group for group, _pages in layout.GROUPS][:-1]
        self.assertEqual(count, len(groups))
        self.assertEqual([group for group, _pages in layout.GROUPS][-1], "mIRC")
        x = b.TAB_START              # after the way back and the side's name
        for number, group in enumerate(groups, start=1):
            tab_x, tab_w = (int(n) for n in self.data["tab.%d" % number].split())
            self.assertEqual(tab_x, x, group)
            self.assertGreaterEqual(tab_w * b.PX_PER_DBU, b.text_px(group) + 2 * b.TAB_PAD * b.PX_PER_DBU, group)
            x += tab_w + b.TAB_GAP
        self.assertLessEqual(x - b.TAB_GAP, b.TABS[0] + b.TABS[2])
        self.assertLessEqual(first + stride * len(groups), b.BODY_DCX_FIRST)
        self.assertEqual(stride, 3)
        # a tab is an outline panel in the line colour, a panel on the face
        # inside it, and a text
        make = alias("dccore.sw.dcxtabs")
        self.assertIn("dccore.sw.dcxline %id 0 0 1 1", make)
        self.assertIn("-c $calc(%id + 1) panel 0 0 1 1", make)
        self.assertIn("-c $calc(%id + 2) text 0 0 1 1 nowrap center noprefix noformat", make)
        self.assertIn("xdid dccore.set $calc(%id + 2) -t $dccore.sw.m(g. $+ %i)", make)
        sel = alias("dccore.sw.dcxtabsel")
        # the chosen tab reaches down over the line, the others end above it
        self.assertIn("%h = $calc(%line - %y + %sel)", sel)
        self.assertIn("%w = $calc($gettok(%r,3,32) - %x)", sel)
        self.assertIn("%face = $iif(%sel,$dccore.sw.s(face),$dccore.sw.s(light))", sel)
        # the outline's region the tab's shape, round at the top, the face's
        # its inside - both hidden while they are moved and shaped, since a
        # region set does not redraw and what was drawn outside the new one
        # would stay on screen
        steps = statements(sel)
        # all three of a tab, the text too: the start page hides them all,
        # and a tab shown again without its name was the operator's first find
        hide = steps.index("dccore.dcx xdid dccore.set $+(%id,-,$calc(%id + 2)) -h")
        self.assertEqual(steps[hide + 1:hide + 9], [
            "dccore.dcx xdid dccore.set %id -p %x %y %w %h",
            "dccore.dcx xdid dccore.set %id -R +p $dccore.sw.dcxpoly(%w,%h,o,2)",
            "dccore.dcx xdid dccore.set $calc(%id + 1) -p %x %y %w %h",
            "dccore.dcx xdid dccore.set $calc(%id + 1) -R +p $dccore.sw.dcxpoly(%w,%h,i,2)",
            "dccore.dcx xdid dccore.set $calc(%id + 1) -C +b %face",
            # the text a line of dialog text tall, in the middle of the tab:
            # the dll the script ships has no vcenter
            "dccore.dcx xdid dccore.set $calc(%id + 2) -p $calc(%x + 2) $calc(%y + 1 + $int($calc((%h - 1 - %th) / 2))) "
            "$calc(%w - 4) %th",
            "dccore.dcx xdid dccore.set $calc(%id + 2) -C +b %face",
            "dccore.dcx xdid dccore.set $+(%id,-,$calc(%id + 2)) -s"])
        self.assertIn("%th = $gettok($dccore.sw.dcxrect(0 8 0 0),2,32)", sel)
        self.assertNotIn("vcenter", code())

    def test_the_start_page_shows_the_bots_figures_from_what_the_side_panel_has(self):
        """Frames under the pills - Bot, Sending, Today, Library - with rows
        of a key and a value, or lines of fixed-width text, filled from the
        status the bot sends for the side panel, and again on every burst."""
        b = self.builder
        self.assertEqual(int(self.data["home.f.n"]), len(b.STATS))
        first, last = (int(n) for n in self.data["home.f.ids"].split("-"))
        self.assertEqual(first, b.STATS_DCX_FIRST)
        self.assertLess(last, b.TAB_DCX_FIRST)
        boxes = len([c for c in self.controls if c[0] == "box"])
        self.assertLessEqual(b.FRAME_DCX_FIRST + boxes * b.FRAME_DCX_STRIDE, first)
        ids, bottoms = [], []
        for number, (column, title, kind, rows) in enumerate(b.STATS, start=1):
            f = self.data["home.f.%d" % number].split()
            ring, tid = int(f[0]), int(f[1])
            rx, ry, rw, rh = (int(n) for n in f[2:6])
            tx, ty, tw, th = (int(n) for n in f[6:10])
            self.assertEqual((f[10], " ".join(f[11:])), (kind, title))
            self.assertEqual(rx, b.STATS_X + column * (b.STATS_W + b.STATS_GAP))
            self.assertEqual((rw, tx, ty, th), (b.STATS_W, rx + b.TITLE_X, ry - b.LINE_DROP, b.TITLE_H))
            self.assertGreaterEqual(ry - b.LINE_DROP, b.HOME_PILL_Y + b.HOME_PILL_H)
            ids += [ring, tid]
            bottoms.append(ry + rh)
            count = int(self.data["home.f.%d.n" % number])
            self.assertEqual(count, len(rows) if kind == "kv" else rows)
            for row_number in range(1, count + 1):
                r = self.data["home.f.%d.r.%d" % (number, row_number)].split()
                key_id, value_id = int(r[0]), int(r[1])
                kx, ky, kw, kh = (int(n) for n in r[2:6])
                self.assertEqual((kx, kh), (rx + b.BOX_PAD, b.LINE_DBU))
                self.assertEqual(ky, ry - b.LINE_DROP + b.BOX_TOP + (row_number - 1) * b.STATS_ROW)
                ids.append(key_id)
                if kind == "kv":
                    vx, vy, vw, vh = (int(n) for n in r[6:10])
                    self.assertEqual((kw, " ".join(r[10:])), (b.STATS_KEY_W, rows[row_number - 1]))
                    self.assertEqual((vx, vy), (kx + kw + 4, ky))
                    self.assertEqual(vx + vw, rx + rw - b.BOX_PAD)
                    ids.append(value_id)
                else:
                    self.assertEqual((value_id, kw, r[6:10]), (0, rw - 2 * b.BOX_PAD, ["0", "0", "0", "0"]))
                    self.assertEqual(len(r), 10)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual((ids[0], ids[-1]), (first, last))
        self.assertLessEqual(max(bottoms), b.TABS[1] + b.TABS[3] - 4)
        # made with the start page, hidden and shown with it, filled when it
        # shows and on every status burst while it does; the lists asked for
        # once, as the Lists page asks
        self.assertEqual(statements(alias("dccore.sw.dcxhome"))[-1], "dccore.sw.dcxstats")
        stats = alias("dccore.sw.dcxstats")
        self.assertIn("dccore.sw.dcxring $gettok(%f,1,32) $dccore.sw.dcxrect($gettok(%f,3-6,32))", stats)
        self.assertIn("-c %v text $dccore.sw.dcxrect($gettok(%r,7-10,32)) nowrap noprefix noformat endellipsis", stats)
        self.assertIn("else { dccore.dcx xdid dccore.set %k -f $dccore.sw.fontflags default 8 Lucida Console }", stats)
        side = statements(alias("dccore.sw.side"))
        self.assertIn("dccore.dcx xdid dccore.set %figures -s", side)
        self.assertIn("dccore.dcx xdid dccore.set %figures -h", side)
        self.assertLess(side.index("dccore.dcx xdid dccore.set %figures -s"), side.index("dccore.sw.home.fill"))
        fill = statements(alias("dccore.sw.home.fill"))
        self.assertEqual(fill[0], "if (!$dialog(dccore.set)) || (!$dccore.sw.s(dcx)) || ($dccore.sw.s(side) != 0) { return }")
        frame = alias("dccore.sw.home.frame")
        for title in ("Bot", "Sending", "Today", "Library"):
            self.assertIn("if ($3 == %s) {" % title, frame)
        self.assertIn("dccore.dcx xdid dccore.set $2 -t Sending $iif(%slots != $null,%used $+ / $+ %slots)", frame)
        self.assertIn("dccore.sw.home.row $1 1 $dccore.sw.home.lists", frame)
        self.assertIn("if ($dialog(dccore.set)) { .timerdccoreSwHome -m 1 300 dccore.sw.home.fill }", alias("dccore.panel.soon"))
        lists = alias("dccore.sw.home.lists")
        self.assertIn("if (!$dccore.sw.s(asked.served)) && ($dccore.sw.can(served)) { dccore.sw.ask served }", lists)
        self.assertIn("dccore.sw.home.fill", handwritten().split("hadd dccore.sws srv.ok 1")[1][:400])

    def test_the_window_opens_on_a_start_page_with_a_side_to_choose_and_a_way_back(self):
        """With DCX: a pill for each side, Bot (the groups with tabs) and mIRC
        (the last group, its pages the list and no strip), on a start page the
        window always opens on; a side has the way back at the strip's left
        end, a pill with an arrow and the side's name beside it."""
        b = self.builder
        first, x, y, w, h = (int(n) for n in self.data["dcxback"].split())
        self.assertEqual((first, x, y, w, h), (b.BACK_DCX_FIRST,) + b.BACK)
        cid, cx, cy, cw, ch = (int(n) for n in self.data["dcxcrumb"].split())
        self.assertEqual((cid, cx, cy, cw, ch), (b.CRUMB_DCX,) + b.CRUMB)
        self.assertLess(x + w, cx)
        self.assertLessEqual(cx + cw, int(self.data["tab.1"].split()[0]))
        self.assertEqual(int(self.data["home.n"]), len(b.HOME))
        pills = []
        for number, (name, line) in enumerate(b.HOME, start=1):
            pid, px, py, pw, ph = (int(n) for n in self.data["home.%d" % number].split())
            self.assertEqual(pid, b.HOME_DCX_FIRST + (number - 1) * b.PILL_STRIDE)
            self.assertEqual((py, pw, ph), (b.HOME_PILL_Y, b.HOME_PILL_W, b.HOME_PILL_H))
            self.assertEqual(self.data["home.%d.t" % number], name)
            self.assertEqual(self.data["home.%d.s" % number], line)
            pills.append(px)
        self.assertEqual(pills[1], pills[0] + b.HOME_PILL_W + b.HOME_PILL_GAP)
        self.assertGreater(pills[0], b.TABS[0])
        self.assertLess(pills[-1] + b.HOME_PILL_W, b.TABS[0] + b.TABS[2])
        self.assertEqual([name for name, _line in b.HOME], ["Bot", "mIRC"])
        self.assertLess(b.BODY_DCX_FIRST, b.BACK_DCX_FIRST)
        self.assertLess(b.HOME_DCX_FIRST + len(b.HOME) * b.PILL_STRIDE, b.DCX_PAGES_ID + 1)
        # made with the rest, after the tabs
        lines = statements(alias("dccore.sw.dcx"))
        self.assertLess(lines.index("dccore.sw.dcxtabs"), lines.index("dccore.sw.dcxhome"))
        home = alias("dccore.sw.dcxhome")
        self.assertIn("dccore.sw.dcxpill $gettok(%b,1,32) %r", home)
        self.assertIn("1 $chr(8592)", home)                       # the arrow, bold
        self.assertIn("-c $gettok(%c,1,32) text $dccore.sw.dcxrect($gettok(%c,2-5,32)) nowrap noprefix noformat", home)
        self.assertIn("dccore.sw.dcxlabel $calc(%id + 2) %x %y %w 1 $dccore.sw.m(home. $+ %i $+ .t)", home)
        self.assertIn("dccore.sw.dcxlabel $calc(%id + 3) %x $calc(%y + %th) %w 0 $dccore.sw.m(home. $+ %i $+ .s)", home)
        self.assertIn("-C +t $dccore.theme.sys(grey)", home)        # COLOR_GRAYTEXT in the light theme
        self.assertIn("return $dccore.dcx(GetSystemColor,COLOR_GRAYTEXT)", alias("dccore.theme.sys"))
        pill = statements(alias("dccore.sw.dcxpill"))
        self.assertEqual(pill[0], "dccore.sw.dcxline $1 $2 $3 $4 $5")
        self.assertIn("dccore.dcx xdid dccore.set $1 -R +p $dccore.sw.dcxpoly($4,$5,o,4)", pill)
        self.assertIn("dccore.dcx xdid dccore.set $calc($1 + 1) -R +p $dccore.sw.dcxpoly($4,$5,i,4)", pill)
        label = alias("dccore.sw.dcxlabel")
        self.assertIn("-c $1 text $2 $3 $4 $dccore.sw.dcxth nowrap center noprefix noformat", label)
        self.assertIn("if ($5) { dccore.dcx xdid dccore.set $1 -f $dccore.sw.fontflags(b) default 8 Tahoma }", label)
        self.assertIn("else { dccore.sw.dcxaa $1 text }", label)

    def test_a_side_shows_its_own_and_hides_the_others(self):
        side = statements(alias("dccore.sw.side"))
        self.assertEqual(side[0], "hadd dccore.sws side $1")
        start = side.index("if ($1 == 0) {")
        # to the start: the page shown goes, with the list, the title and the
        # help; the way back and the tabs too; the pills come
        self.assertEqual(side[start + 1], "if ($dccore.sw.s(page)) { dccore.sw.showpage $dccore.sw.s(page) -h | hdel dccore.sws page }")
        self.assertIn("dccore.dcx xdid dccore.set %way -h", side[start:start + 8])
        self.assertIn("dccore.dcx xdid dccore.set %tabs -h", side[start:start + 8])
        self.assertIn("dccore.dcx xdid dccore.set %list -h", side[start:start + 8])
        self.assertIn("dccore.dcx xdid dccore.set %title -h", side[start:start + 8])
        self.assertIn("did -h dccore.set 1012", side[start:start + 8])
        self.assertIn("dccore.dcx xdid dccore.set %pills -s", side[start:start + 8])
        # a side: the pills go, the way back with its name comes, and the bot's
        # tab last shown or the mIRC group - the last - with no tabs
        after = side[start + 8:]
        self.assertIn("dccore.dcx xdid dccore.set %pills -h", after)
        self.assertIn("dccore.dcx xdid dccore.set $gettok($dccore.sw.m(dcxcrumb),1,32) -t $iif($1 == 1,Bot,mIRC)", after)
        self.assertIn("dccore.dcx xdid dccore.set %way -s", after)
        self.assertIn("if ($1 == 2) { dccore.dcx xdid dccore.set %tabs -h | dccore.sw.tab $dccore.sw.m(groups) | return }", after)
        self.assertEqual(after[-1], "dccore.sw.tab $iif(%bt,%bt,1)")
        self.assertIn("if ($1 < $dccore.sw.m(groups)) { hadd dccore.sws bottab $1 }", alias("dccore.sw.tab"))
        self.assertEqual(statements(alias("dccore.sw.dcxtabsel"))[0], "if ($dccore.sw.s(side) == 2) { return }")
        # a pill or the way back clicked goes through dccore.sw.go, which asks
        # about changes not saved as a tab does; a jump to a setting's page
        # enters its side first
        cb = statements(alias("dccore.sw.dcxcb"))
        self.assertIn("if ($3 >= %b) && ($3 < $calc(%b + 3)) { dccore.sw.go side 0 | return }", cb)
        self.assertIn("if ($3 >= %h) && ($3 < $calc(%h + 4 * $dccore.sw.m(home.n))) { dccore.sw.go side "
                      "$calc($int($calc(($3 - %h) / 4)) + 1) | return }", cb)
        go = statements(alias("dccore.sw.go"))
        self.assertEqual(go[0], "if ($1 == side) && ($2 == $dccore.sw.s(side)) { return }")
        self.assertIn("if ($1 == side) { dccore.sw.side $2 }", statements(alias("dccore.sw.goon")))
        self.assertIn("if ($dccore.sw.s(dcx)) && ($dccore.sw.s(side) != $iif(%g == $dccore.sw.m(groups),2,1)) "
                      "{ dccore.sw.side $iif(%g == $dccore.sw.m(groups),2,1) }", alias("dccore.sw.goto"))
        # the native form has none of it: its tabs are the groups, the mIRC
        # group the last
        table = block("dialog dccore.set {", raw())
        for key in ("dcxback", "dcxcrumb", "home."):
            self.assertNotIn(key, table)
        self.assertIn('tab "mIRC", 1007', table)

    def test_the_corners_are_the_midpoint_circles_pixels_and_the_polygons_cut_exactly_them(self):
        """The ring a region on pixel edges holds is exactly the pixels in
        it, so the outside polygon less the inside one is the arc pixels
        and the lines, nothing more or less - which GDI's rounded rectangle
        region did not give. Rasterised here as GDI does, by pixel centre."""
        b = self.builder
        radius = b.ROUND_PX
        arc = b.arc_pixels(radius)
        # a quarter circle from the left edge to the top edge, one pixel a row
        # and a column, no two side by side at the ends
        self.assertIn((0, radius), arc)
        self.assertIn((radius, 0), arc)
        self.assertEqual(sorted(set(y for _x, y in arc)), list(range(radius + 1)))
        self.assertEqual(sorted(set(x for x, _y in arc)), list(range(radius + 1)))
        outer, inner = b.corner_traces(radius)

        def polygon(trace, w, h, corners):
            tl = list(trace)
            tr = [(w - x, y) for x, y in reversed(tl)]
            br = [(w - x, h - y) for x, y in tl]
            bl = [(x, h - y) for x, y in reversed(tl)]
            if corners == 2:
                e = tl[0][0]
                return [(e, h)] + tl + tr + [(w - e, h)]
            return tl + tr + br + bl

        def inside(polygon_points, px, py):
            x, y, hit = px + 0.5, py + 0.5, False
            for i in range(len(polygon_points)):
                (x1, y1), (x2, y2) = polygon_points[i], polygon_points[(i + 1) % len(polygon_points)]
                if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    hit = not hit
            return hit

        w, h = 40, 31
        for corners in (4, 2):
            out_poly, in_poly = polygon(outer, w, h, corners), polygon(inner, w, h, corners)
            ring = {(x, y) for x in range(w) for y in range(h)
                    if inside(out_poly, x, y) and not inside(in_poly, x, y)}
            expected = {(x, 0) for x in range(radius + 1, w - radius - 1)}
            expected |= {(x, y) for x, y in arc} | {(w - 1 - x, y) for x, y in arc}
            if corners == 4:
                expected |= {(x, h - 1) for x in range(radius + 1, w - radius - 1)}
                expected |= {(x, h - 1 - y) for x, y in arc} | {(w - 1 - x, h - 1 - y) for x, y in arc}
                sides = range(radius + 1, h - radius - 1)
            else:
                sides = range(radius + 1, h)
            expected |= {(0, y) for y in sides} | {(w - 1, y) for y in sides}
            self.assertEqual(ring, expected, corners)
        self.assertIn("if ($dccore.sw.s(dcx)) { dccore.sw.dcxpages %pages | dccore.sw.dcxtabsel }", alias("dccore.sw.tab"))
        # a click on a tab, its outline or its text, goes through dccore.sw.go
        cb = statements(alias("dccore.sw.dcxcb"))
        self.assertIn("if (%g <= $dccore.sw.m(groups)) { dccore.sw.go tab %g | return }", cb)

    def test_every_frame_is_drawn_with_dcx_from_its_box_and_shown_with_its_page(self):
        b = self.builder
        boxes = [c for c in self.controls if c[0] == "box"]
        expect = b.FRAME_DCX_FIRST
        for _kind, label, cid, x, y, w, h, _style in boxes:
            title = label.replace("&&", "&")
            f = self.data["fr.%d" % cid].split()
            self.assertEqual(int(f[0]), expect, cid)
            expect += b.FRAME_DCX_STRIDE
            # the ring under the title's middle, the title over its top line
            self.assertEqual([int(n) for n in f[1:5]], [x, y + b.LINE_DROP, w, h - b.LINE_DROP], cid)
            tx, ty, tw, th = (int(n) for n in f[5:9])
            self.assertEqual((tx, ty, th), (x + b.TITLE_X, y, b.TITLE_H), cid)
            self.assertGreaterEqual(tw * b.PX_PER_DBU, b.text_px(title) + 2 * b.TITLE_PAD * b.PX_PER_DBU, cid)
            self.assertLessEqual(tx + tw, x + w - b.TITLE_X, cid)
            self.assertEqual(" ".join(f[9:]), title, cid)
        self.assertLessEqual(expect, b.TAB_DCX_FIRST)
        for page in self.pages:
            ids = [c[2] for c in boxes if self.page_of(c[2]) == page]
            if not ids:
                self.assertNotIn("p.%d.bx" % page, self.data)
                continue
            self.assertEqual(self.data["p.%d.bx" % page], ",".join(str(i) for i in ids))
            first, last = (int(n) for n in self.data["p.%d.fr" % page].split("-"))
            self.assertEqual(first, int(self.data["fr.%d" % ids[0]].split()[0]))
            self.assertEqual(last, int(self.data["fr.%d" % ids[-1]].split()[0]) + b.FRAME_DCX_STRIDE - 1)
        frames = statements(alias("dccore.sw.dcxframes"))
        self.assertIn("dccore.sw.dcxring %base $dccore.sw.dcxrect($gettok(%f,2-5,32))", frames)
        self.assertIn("dccore.dcx xdialog dccore.set -c %title text $dccore.sw.dcxrect($gettok(%f,6-9,32)) nowrap center "
                      "noprefix noformat", frames)
        self.assertIn("dccore.dcx xdid dccore.set %title -C +b $dccore.sw.s(face)", frames)
        self.assertIn("dccore.dcx xdid dccore.set %title -t $gettok(%f,10-,32)", frames)
        self.assertIn("dccore.dcx xdid dccore.set $+(%base,-,%title) -h", frames)
        # made once a window (#1382): a DCX id made twice is an error
        self.assertEqual(frames[:2], ["if ($dccore.sw.s(frmade. $+ $1)) { return }", "hadd dccore.sws frmade. $+ $1 1"])
        # the native boxes stay hidden; the frames made as the page is first
        # shown, and shown or hidden with it only once they are made
        show = statements(alias("dccore.sw.showpage"))
        bx = show.index("if (%bx != $null) {")
        self.assertEqual(show[bx + 1:bx + 5], [
            "did -h dccore.set %bx",
            "if ($2 == -v) { dccore.sw.dcxframes $1 }",
            "if ($dccore.sw.s(frmade. $+ $1)) { dccore.dcx xdid dccore.set $dccore.sw.m(p. $+ $1 $+ .fr) "
            "$iif($2 == -v,-s,-h) }",
            "}"])

    def test_every_table_has_its_columns_and_a_listview_over_its_place(self):
        lists = [c for c in self.controls if c[0] == "list"]
        self.assertEqual(sorted(c[2] for c in lists), sorted(TABLES))
        self.assertEqual([int(i) for i in self.data["lvs"].split()], [c[2] for c in lists])
        dcx_ids = []
        for _kind, _label, cid, x, y, w, h, _style in lists:
            dcx_id, lx, ly, lw, lh = (int(n) for n in self.data["lv.%d" % cid].split())
            self.assertEqual((lx, ly, lw, lh), (x, y, w, h), cid)
            self.assertTrue(self.builder.DCX_TITLE_ID < dcx_id < 1001, dcx_id)
            dcx_ids.append(dcx_id)
            columns = [col.split(" ", 1) for col in self.data["lvc.%d" % cid].split(",")]
            self.assertTrue(all(title for _w, title in columns), cid)
            self.assertEqual([w for w, _t in columns].count("0"), 1)
            self.assertEqual(columns[-1][0], "0")
            self.assertIn(str(cid), self.data["p.%d.lv" % self.page_of(cid)].split())
        self.assertEqual(len(set(dcx_ids)), len(dcx_ids))
        self.assertEqual(self.data["lvx.1520"], "noheader")
        self.assertEqual(self.data["lvc.1540"], "120 Name,44 Primary,0 Folders")

    def test_a_listview_is_made_hidden_over_its_native_list_with_its_columns(self):
        lines = statements(alias("dccore.sw.lv.make"))
        self.assertEqual(lines[1], "did -h dccore.set $1")
        self.assertIn("dccore.dcx xdialog dccore.set -c %id listview %rect report singlesel fullrow showsel "
                      "$dccore.sw.m(lvx. $+ $1)", lines)
        self.assertIn("dccore.dcx xdid dccore.set %id -t %out", lines)
        # hidden last of all it is told: made when first needed (#1382), its
        # font, state and colours come before it
        told = [n for n, s in enumerate(lines) if "dccore.set %id " in s]
        self.assertEqual(lines[told[-1]], "dccore.dcx xdid dccore.set %id -h")
        self.assertLess(lines.index("dccore.dcx xdid dccore.set %id -t %out"), told[-1])
        for given in ("dccore.sw.dcxaa %id listview", "if ($dccore.sw.s(lv.e) == -b) { dccore.dcx xdid dccore.set %id -b }",
                      "if ($dccore.sw.s(dark)) { dccore.theme.dcxlist %id }"):
            self.assertLess(lines.index(given), told[-1], given)
        self.assertEqual(lines[-1], "if ($dccore.sw.s(inited)) && ($dccore.sw.s(noct)) "
                                    "{ .timerdccoreThemeLv -m 1 250 dccore.sw.lv.dark }")
        # the last column takes what the others leave
        self.assertIn("if (%w == 0) { %w = %left }", lines)

    def test_the_tables_are_only_touched_through_the_helpers(self):
        text = handwritten()
        ids = "|".join(str(i) for i in TABLES)
        self.assertNotRegex(text, r"did -[a-z]+ dccore\.set (?:%s)\b" % ids)
        self.assertNotRegex(text, r"\$did\(dccore\.set,(?:%s)[,)]" % ids)
        for name in ("clear", "add", "sel", "count", "text", "del"):
            body = alias("dccore.sw.lv." + name)
            self.assertIn("$dccore.sw.s(dcx)", body, name)
            self.assertIn("$dccore.sw.lv.id($1)", body, name)
        # the listview over it, made the first time it is needed (#1382)
        self.assertEqual(statements(alias("dccore.sw.lv.id")),
                         ["if (!$dccore.sw.s(dcx)) { return $1 }",
                          "dccore.sw.lv.need $1",
                          "return $gettok($dccore.sw.m(lv. $+ $1),1,32)"])
        self.assertEqual(statements(alias("dccore.sw.lv.need")),
                         ["if (!$dccore.sw.s(dcx)) || ($dccore.sw.s(lvmade. $+ $1)) { return }",
                          "hadd dccore.sws lvmade. $+ $1 1",
                          "dccore.sw.lv.make $1"])

    def test_a_row_and_a_selection_say_the_same_to_either(self):
        add = alias("dccore.sw.lv.add")
        self.assertIn("dccore.dcx xdid dccore.set $dccore.sw.lv.id($1) -a 0 0 + 0 0 0 0 0 0 %row", add)
        self.assertIn("else { did -a dccore.set $1 %row }", add)
        self.assertIn("var %cell = $dccore.sw.untok($gettok($2-,%i,32))", add)
        sel = alias("dccore.sw.lv.sel")
        self.assertIn("$dccore.dcx(_xdid,dccore.set $dccore.sw.lv.id($1) sel)", sel)
        self.assertIn("return $did(dccore.set,$1).sel", sel)
        self.assertIn("return $iif(%s isnum 1-,%s)", sel)

    def test_the_listviews_follow_their_page_and_the_enable_state(self):
        show = alias("dccore.sw.showpage")
        self.assertIn("var %lvs = $dccore.sw.m(p. $+ $1 $+ .lv), %i = 1", show)
        self.assertIn("var %lv = $gettok(%lvs,%i,32)", show)
        self.assertIn("did -h dccore.set %lv", show)
        # shown makes it (lv.id), hidden only once it was made (#1382)
        self.assertIn("if ($2 == -v) || ($dccore.sw.s(lvmade. $+ %lv)) { dccore.dcx xdid dccore.set "
                      "$dccore.sw.lv.id(%lv) $iif($2 == -v,-s,-h) }", show)
        # a disabled listview is drawn light grey whatever its colours, so in
        # the dark theme (#1368) the tables stay enabled: their rows do
        # nothing by themselves, the buttons that act on them are disabled
        enable = alias("dccore.sw.enable")
        self.assertIn("%e = $iif(($1) || ($dccore.theme.dialogsdark),-e,-b)", enable)
        # a table not made yet is left alone; lv.make gives it lv.e (#1382)
        self.assertIn("hadd dccore.sws lv.e %e", enable)
        self.assertIn("if ($dccore.sw.s(lvmade. $+ $gettok(%lvs,%i,32))) { dccore.dcx xdid dccore.set "
                      "$dccore.sw.lv.id($gettok(%lvs,%i,32)) %e }", enable)
        # made after every page was hidden, before the first is shown
        init = statements(alias("dccore.sw.init"))
        self.assertLess(init.index("dccore.sw.enable 0"), init.index("dccore.sw.dcx"))

    def test_the_lists_tables_selection_is_read_when_a_button_needs_it(self):
        # no pick on a click: Edit and Delete read the selected row then
        self.assertEqual(statements(alias("dccore.sw.srv.selected")),
                         ["return $dccore.sw.s(srv.rowl. $+ $dccore.sw.lv.sel(1540))"])
        self.assertNotIn("pick", alias("dccore.sw.dcxcb"))
        click = alias("dccore.sw.click")
        self.assertIn("if (%id == 1554) { if ($dccore.sw.srv.selected != $null) { dccore.sw.srv.open $v1 }", click)
        self.assertIn("var %n = $dccore.sw.srv.selected", alias("dccore.sw.srv.delete"))

    def test_lists_and_bindings_are_the_mockups_two_pages(self):
        import settings_window_layout as layout
        sharing = dict(dict(layout.GROUPS)["Sharing"])
        widgets_of = lambda page: [(h, [i["widget"] for i in items if isinstance(i, dict) and "widget" in i])
                                   for h, items in page]
        self.assertEqual(widgets_of(sharing["Lists"]), [("", []), ("Lists", ["srvlists"])])
        self.assertEqual(widgets_of(sharing["Channel bindings"]),
                         [("", []), ("Channel bindings", ["srvchans"]), ("", ["srvsaveb"])])
        # the lists table: a row per list, Edit and Add list opening the list window
        draw = statements(alias("dccore.sw.srv.draw"))
        self.assertEqual(draw[0], "dccore.sw.lv.clear 1540")
        self.assertIn("hadd dccore.sws srv.rowl. $+ %i %id", draw)
        self.assertEqual(draw[-1], "dccore.sw.srv.drawrows")
        buttons = {c[2]: c[1] for c in self.controls if c[0] == "button"}
        self.assertEqual([buttons[i] for i in (1551, 1554, 1555, 1557)], ["Add list", "Edit", "Delete", "Refresh"])
        click = alias("dccore.sw.click")
        self.assertIn("if (%id == 1551) { dccore.sw.srv.open new | return }", click)
        self.assertIn("if (%id == 1555) { dccore.sw.srv.delete | return }", click)
        self.assertIn("if (%id == 1557) || (%id == 1567) { dccore.sw.reask served | return }", click)
        # no Save button of its own: Apply saves the lists with everything else
        self.assertFalse([c for c in self.controls if c[0] == "button" and c[1].startswith("Save")])
        self.assertNotIn("dccore.sw.srv.save", click)
        for name in ("pickl", "pickc", "sellist", "newfolder", "changel", "changec", "removel", "removec", "addlist"):
            self.assertNotIn("alias dccore.sw.srv.%s " % name, code(), name)
        # the list window: its own dialog, its folders typed there
        table = block("dialog dccore.setl {")
        for cid, text in ((2, "edit"), (3, "check"), (5, "list"), (7, "edit"), (9, "edit"), (15, "OK"), (16, "Cancel")):
            self.assertRegex(table, r"(?m)^  \w+ (?:\"[^\"]*\", )?%d, " % cid, text)
        self.assertIn("on *:dialog:dccore.setl:sclick:*: { dccore.sw.srv.lclick $did }", code())
        lok = alias("dccore.sw.srv.lok")
        self.assertIn("hadd dccore.sws srv.l. $+ %id $did(dccore.setl,3).state $dccore.sw.enc($did(dccore.setl,2).text)", lok)
        self.assertIn("dccore.sw.srv.addfolder %id $hget(dccore.swl,f. $+ $gettok(%w,%i,32))", lok)
        self.assertEqual(statements(lok)[-1], "dccore.sw.srv.edited")
        # and nothing reaches the bot but through Apply
        self.assertNotIn("dccore.send", alias("dccore.sw.srv.lok"))
        self.assertNotIn("dccore.send", alias("dccore.sw.srv.rowclick"))

    def test_the_bindings_rows_are_the_mockups(self):
        import build_settings_window as builder
        page = next(p for p in self.pages if untext(self.data["p.%d" % p]) == "Channel bindings")
        ids = set(self.pages[page])
        for r in range(builder.BIND_ROWS):
            base = builder.BIND_FIRST_ID + 4 * r
            kinds = [self.where[base + k][0] for k in range(4)]
            self.assertEqual(kinds, ["text", "combo", "combo", "button"], r)
            self.assertEqual(self.where[base + 3][1], "Remove")
            self.assertTrue({base, base + 1, base + 2, base + 3} <= ids, r)
            if r:
                self.assertEqual(self.where[base][4] - self.where[base - 4][4], builder.BIND_PITCH)
        add = builder.BIND_ADD_ID
        self.assertEqual([self.where[add + k][0] for k in range(4)], ["edit", "combo", "combo", "button"])
        self.assertEqual(self.where[add + 3][1], "Add")
        self.assertEqual(self.where[1567][1], "Refresh")
        self.assertNotIn(1566, self.where)
        self.assertEqual(statements(alias("dccore.sw.srv.rowbase")), ["return $calc(1700 + 4 * ($1 - 1))"])
        self.assertEqual(builder.BIND_FIRST_ID, 1700)
        # a scrollbar beside the rows, not pages: every channel is reached by
        # scrolling, and the rows are filled from its position
        scroll = self.where[builder.BIND_SCROLL_ID]
        self.assertEqual(scroll[0], "scroll")
        self.assertIn(builder.BIND_SCROLL_ID, ids)
        self.assertGreater(scroll[3], self.where[builder.BIND_FIRST_ID + 3][3] + self.where[builder.BIND_FIRST_ID + 3][5])
        self.assertIn("vertical", scroll[7])
        self.assertIn("on *:dialog:dccore.set:scroll:*: { dccore.sw.scrolled $did }", code())
        self.assertEqual(statements(alias("dccore.sw.scrolled")),
                         ["if ($1 != 1774) { return }", "hadd dccore.sws bind.off $did(dccore.set,1774).sel",
                          "dccore.sw.srv.drawrows"])
        drawrows = alias("dccore.sw.srv.drawrows")
        self.assertIn("did -z dccore.set 1774 range 0 $iif(%n > 15,$calc(%n - 15),0)", drawrows)
        self.assertIn("did -c dccore.set 1774 %off", drawrows)
        self.assertIn("if (%off > $calc(%n - 15)) { %off = $calc(%n - 15) }", drawrows)
        self.assertNotIn("Previous", code())
        self.assertNotIn("pagenext", code())
        # a click in a row: its menus change the binding at once, its Remove takes it
        click = alias("dccore.sw.click")
        self.assertIn("if (%id isnum 1700-1759) { dccore.sw.srv.rowclick %id | return }", click)
        self.assertIn("if (%id == 1773) { dccore.sw.srv.newchan | return }", click)
        rowclick = alias("dccore.sw.srv.rowclick")
        self.assertIn("if (%what == 3) {", rowclick)
        self.assertIn("hadd dccore.sws srv.c. $+ %n $dccore.sw.srv.chosen($calc(%base + 1)) $gettok(%row,2,32) "
                      "$dccore.sw.srv.mode($calc(%base + 2))", rowclick)
        self.assertIn("hadd dccore.sws srv.dirty 1", rowclick)
        # the spare rows are hidden, and hidden again each time the page is shown
        rowsvis = statements(alias("dccore.sw.srv.rowsvis"))
        self.assertIn("did $iif($calc(%off + %r) <= %n,-v,-h) dccore.set", "\n".join(rowsvis))
        # and no row is shown while another page is up: the lists arrive from
        # the bot after they were asked for, and were drawn over whatever
        # page was shown by then
        self.assertEqual(rowsvis[0], "if (!$istok($dccore.sw.m(p. $+ $dccore.sw.s(page) $+ .w),srvchans,32)) { return }")
        shows = [line for line in handwritten().split("\n") if "did -v dccore.set" in line or "-v,-h" in line]
        for line in shows:
            # (1012 is the help under the page, shown with a side, not a page's)
            self.assertTrue("dccore.sw.showpage" in line or "$calc(1019 + %i)" in line
                            or "-v,-h" in line or "did -v dccore.set 1012" in line, line)
        self.assertIn("if ($istok($dccore.sw.m(p. $+ $1 $+ .w),srvchans,32)) { dccore.sw.srv.rowsvis }",
                      alias("dccore.sw.page"))
        self.assertIn("srvchans", self.data["p.%d.w" % page].split())
        # the row that adds one offers the first list and mode, never line 0
        draw = alias("dccore.sw.srv.drawrows")
        self.assertIn("dccore.sw.pickline 1771 $iif($dccore.sw.s(srv.order) != $null,1,0)", draw)
        self.assertIn("if (!$did(dccore.set,1772).sel) { did -c dccore.set 1772 1 }", draw)
        # every Mode menu gets the modes once, at init
        combos = alias("dccore.sw.combos")
        self.assertIn("while (%r <= 15) { dccore.sw.fillfrom $calc($dccore.sw.srv.rowbase(%r) + 2)", combos)
        self.assertIn("dccore.sw.fillfrom 1772", combos)

    def test_the_names_and_paths_shown_are_the_stored_ones_made_tokens(self):
        draw = alias("dccore.sw.srv.draw")
        self.assertIn("$dccore.sw.retok($gettok(%l,2-,32))", draw)
        self.assertIn("$dccore.sw.tok(%folders)", draw)
        self.assertEqual(statements(alias("dccore.sw.retok")), ["return $replace($1-,$chr(32),$chr(37) $+ 20)"])


class ApplySavesEverything(unittest.TestCase):
    """The operator: a Save button beside Apply is easy to miss, and easy to
    take for the one that saves. So the pages have none, Apply sends every
    kind of change, and leaving a page with changes not saved asks first."""

    def test_the_pages_have_no_save_buttons(self):
        controls = dialog("dccore.set")
        self.assertEqual([c[1] for c in controls if c[0] == "button" and c[1].lower().startswith("save")], [])
        click = alias("dccore.sw.click")
        self.assertNotIn("dccore.sw.srv.save", click)
        self.assertNotIn("dccore.sw.oc.save", click)

    def test_apply_sends_the_lists_and_the_on_connect_commands_too(self):
        lines = statements(alias("dccore.sw.apply"))
        oc = lines.index("if ($dccore.sw.ocdirty) && (!$dccore.sw.s(saving.oc)) { dccore.sw.oc.save | inc %sent }")
        srv = lines.index("if ($dccore.sw.s(srv.dirty)) && (!$dccore.sw.s(saving.srv)) { dccore.sw.srv.save | inc %sent }")
        keys = lines.index("var %keys = $iif($dccore.sw.s(loaded),$dccore.sw.changed)")
        self.assertLess(oc, keys)
        self.assertLess(srv, keys)
        # with nothing else to send, OK waits for those replies instead of closing at once
        self.assertIn("if (%sent) { dccore.sw.maybeclose | return }", lines)
        self.assertLess(lines.index("if (%sent) { dccore.sw.maybeclose | return }"),
                        lines.index("if ($1 == close) { dialog -x dccore.set | return }"))
        self.assertNotIn("has changes of its own not saved yet", alias("dccore.sw.apply"))

    def test_every_reply_closes_or_keeps_the_window(self):
        body = alias("dccore.sw.line")
        srv = body[body.index("if (%t == SRVDONE) || (%t == FLDDONE) {"):body.index("if (%t == FLDBEGIN)")]
        self.assertIn("if ($2 == ok) { hdel dccore.sws srv.dirty | dccore.sw.status $4- | dccore.sw.reask served | "
                      "dccore.sw.maybeclose | return }", srv)
        self.assertIn("if ($2 == unchanged) { hdel dccore.sws srv.dirty | dccore.sw.status $3- | dccore.sw.maybeclose | return }", srv)
        self.assertIn("if ($2 == error) { hdel dccore.sws close | dccore.sw.status Not saved:", srv)
        oc = body[body.index("if (%t == OCDONE) {"):body.index("if (%t == OCRESEND) {")]
        self.assertIn("if ($2 == ok) { dccore.sw.status $4- | dccore.sw.reask onconnect | dccore.sw.maybeclose | return }", oc)
        self.assertIn("if ($2 == unchanged) { dccore.sw.status $3- | dccore.sw.maybeclose | return }", oc)
        self.assertIn("if ($2 == error) { hdel dccore.sws close | dccore.sw.status Not saved:", oc)
        self.assertIn("hdel dccore.sws close", alias("dccore.sw.riskyask"))

    def test_leaving_a_page_with_changes_asks_first(self):
        click = alias("dccore.sw.click")
        self.assertIn("if (%id isnum 1001-1007) { dccore.sw.go tab $calc(%id - 1000) | return }", click)
        self.assertIn("if (%p) { dccore.sw.go page %p }", click)
        self.assertEqual(statements(alias("dccore.sw.dcxcb"))[-1], "dccore.sw.go page %p")
        go = statements(alias("dccore.sw.go"))
        self.assertIn("if (!$dccore.sw.anydirty) { dccore.sw.goon $1 $2 | return }", go)
        self.assertEqual(go[-3:], ["hadd dccore.sws going $1 $2", "dccore.sw.stay",
                                   ".timerdccoreSwAsk -m 1 0 dccore.sw.leaveask"])
        ask = alias("dccore.sw.leaveask")
        self.assertIn("if ($input(You have changes that are not saved. Save them before leaving this page? "
                      "Yes saves them and goes on; No stays here.,yq,DCCore - Control Panel)) "
                      "{ dccore.sw.apply apply | dccore.sw.goon %go }", ask)
        # the page the bot refused a setting on is shown without asking
        self.assertIn("dccore.sw.tab %g", alias("dccore.sw.goto"))
        self.assertNotIn("dccore.sw.go ", alias("dccore.sw.goto"))

    def test_what_counts_as_not_saved(self):
        lines = statements(alias("dccore.sw.anydirty"))
        self.assertEqual(lines, ["if ($dccore.sw.s(srv.dirty)) || ($dccore.sw.ocdirty) { return $true }",
                                 "if ($dccore.sw.s(loaded)) && ($dccore.sw.changed != $null) { return $true }",
                                 "if ($dccore.sw.localdirty) { return $true }",
                                 "return $dccore.sw.opt.dirty"])
        local = alias("dccore.sw.localdirty")
        self.assertIn("if ($did(dccore.set,$dccore.sw.m(lo. $+ %o)).state != $iif($dccore.opt(%o),1,0)) { return $true }",
                      local)
        # staying puts the page shown back, in either form - and the native
        # tab in the native form, where the tab strip is Windows' own
        stay = statements(alias("dccore.sw.stay"))
        self.assertEqual(stay[0], "if ($dccore.sw.s(dcx)) { dccore.sw.dcxshow $dccore.sw.s(page) | return }")
        self.assertEqual(stay[1], "did -f dccore.set $calc(1000 + $dccore.sw.s(tab))")
        self.assertIn("if (%at) { did -c dccore.set $calc(1019 + %at) }", stay)


if __name__ == "__main__":
    unittest.main()
